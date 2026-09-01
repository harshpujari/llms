"""Low-Rank Adaptation (LoRA) for the tutorial's transformer.

Full fine-tuning updates all 13.8M parameters and writes a 165 MB checkpoint per run.
LoRA freezes the pre-trained weights and trains a small additive correction instead: for a
frozen `W` of shape (out, in), it learns `A` of shape (rank, in) and `B` of shape
(out, rank), and computes

    y = W x + (B A) x * (alpha / rank)

`B A` has the same shape as `W` but is constrained to rank `rank`, so it is described by
`rank * (in + out)` numbers instead of `in * out`. At rank 4 on this model that is 49,152
trainable parameters against 13.8M -- 0.36% -- and an adapter file of a few hundred KB
rather than 165 MB.

**`B` starts at zero, `A` at random.** That is not a detail, it is the whole reason this is
safe to bolt onto a trained model: `B A` is exactly zero at initialisation, so the adapted
model's outputs are *identical* to the frozen one's until training moves them. Initialising
both randomly would inject noise into a working model on step 0. `Notebooks/7_...ipynb`
asserts the identity rather than trusting this docstring.

The rank/alpha split is a convention worth knowing: `alpha` is a fixed numerator so that
changing `rank` does not silently rescale the update. Doubling rank halves the per-unit
contribution, keeping the effective step size roughly stable.

What LoRA does *not* buy is a 300x speedup. The frozen forward pass still runs in full, so
the parameter ratio is a memory and storage number, never a time one. It is *some* speedup:
measured on this model, 0.75 s/step against 1.17 for full fine-tuning, about 1.6x. That comes
from skipping the weight-gradient matmul on every frozen layer and from AdamW updating 49k
tensors' worth of state instead of 13.8M -- not from doing less forward arithmetic. On a
larger model, where the forward and backward passes dominate the optimiser step, the same
technique saves proportionally less time and just as much memory.
"""

from __future__ import annotations

import math
from typing import Iterable

import torch
import torch.nn as nn
import torch.nn.functional as F

# The attention projections, which is what the LoRA paper adapts. `qkv` is fused here, so
# wrapping it adapts the query, key and value projections at once.
DEFAULT_TARGETS: tuple[str, ...] = ("qkv", "projection")


class LoRALinear(nn.Module):
    """A frozen `nn.Linear` plus a trainable low-rank correction.

    The original layer is kept as a child module rather than copied, so the pre-trained
    weight is shared, never duplicated, and stays visible to `state_dict()` under its
    original name prefix.
    """

    def __init__(self, base: nn.Linear, rank: int, alpha: float, dropout: float = 0.0) -> None:
        super().__init__()
        if rank <= 0:
            raise ValueError(f"rank must be positive, got {rank}")

        self.base = base
        for parameter in self.base.parameters():
            parameter.requires_grad_(False)

        self.rank = rank
        self.alpha = alpha
        self.scaling = alpha / rank

        # A is (rank, in), B is (out, rank). B starts at zero so B@A is zero: the wrapped
        # layer returns exactly what the frozen one did until training changes it.
        self.lora_a = nn.Parameter(torch.empty(rank, base.in_features))
        self.lora_b = nn.Parameter(torch.zeros(base.out_features, rank))
        nn.init.kaiming_uniform_(self.lora_a, a=math.sqrt(5))

        self.lora_dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()
        self.merged = False

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.base(x)
        if self.merged:
            return out                      # the update is already inside base.weight
        update = F.linear(F.linear(self.lora_dropout(x), self.lora_a), self.lora_b)
        return out + update * self.scaling

    @torch.no_grad()
    def merge(self) -> None:
        """Fold the correction into the frozen weight, removing all inference overhead.

        After merging the layer is an ordinary Linear again, arithmetically. Useful before
        deployment; irreversible in the sense that the adapter can no longer be swapped out
        without re-loading the base weights.
        """
        if not self.merged:
            self.base.weight.add_((self.lora_b @ self.lora_a) * self.scaling)
            self.merged = True

    def extra_repr(self) -> str:
        return f"rank={self.rank}, alpha={self.alpha}, merged={self.merged}"


def _replace_module(root: nn.Module, qualified_name: str, replacement: nn.Module) -> None:
    """Swap the module at a dotted path for another, in place."""
    *parents, attribute = qualified_name.split(".")
    parent = root
    for step in parents:
        parent = getattr(parent, step)
    setattr(parent, attribute, replacement)


def get_lora_model(
    model: nn.Module,
    lora_config: dict | None = None,
    device: str | torch.device | None = None,
) -> nn.Module:
    """Freeze `model` and wrap its target linear layers with LoRA adapters.

    The model is modified **in place** and returned, which is what every PEFT library does
    and is worth stating plainly: the returned object *is* the one passed in, not a copy.
    A caller holding the original reference now holds an adapted model too.

    Args:
        model: the network to adapt. Any `torch.compile` wrapper is unwrapped first.
        lora_config: `rank`, `alpha`, optional `dropout`, and optional `target_modules`
            (a tuple of attribute names to match, defaulting to the attention projections).
        device: where to put the newly created adapter parameters.

    Returns:
        The same model, with every target `nn.Linear` replaced by a `LoRALinear` and every
        other parameter frozen.
    """
    config = {"rank": 4, "alpha": 8, "dropout": 0.0, "target_modules": DEFAULT_TARGETS}
    config.update(lora_config or {})

    # torch.compile wraps the module; adapting the wrapper would leave the real layers
    # untouched and silently train nothing.
    model = getattr(model, "_orig_mod", model)

    for parameter in model.parameters():
        parameter.requires_grad_(False)

    targets = tuple(config["target_modules"])
    replaced = []
    for name, module in list(model.named_modules()):
        if isinstance(module, nn.Linear) and name.split(".")[-1] in targets:
            adapter = LoRALinear(
                module, rank=config["rank"], alpha=config["alpha"],
                dropout=config["dropout"],
            )
            _replace_module(model, name, adapter)
            replaced.append(name)

    if not replaced:
        raise ValueError(
            f"no linear layers matched target_modules={targets}. "
            f"Available leaf names: "
            f"{sorted({n.split('.')[-1] for n, m in model.named_modules() if isinstance(m, nn.Linear)})}"
        )

    if device is not None:
        model.to(device)
    return model


def lora_parameters(model: nn.Module) -> Iterable[nn.Parameter]:
    """Just the adapter parameters - what the optimiser should actually be given.

    Passing `model.parameters()` to AdamW instead still *trains* correctly, because the
    frozen tensors have `requires_grad=False` and receive no gradient. It just allocates
    optimiser state for 13.8M parameters to update 49k of them, which throws away most of
    the memory saving LoRA exists for.
    """
    return (p for p in model.parameters() if p.requires_grad)


def lora_state_dict(model: nn.Module) -> dict[str, torch.Tensor]:
    """Only the adapter tensors - the checkpoint that makes LoRA worth using.

    The base weights are unchanged by definition, so saving them again per run is pure
    duplication. This is the difference between a 165 MB checkpoint and a few hundred KB.
    """
    model = getattr(model, "_orig_mod", model)
    return {
        name: tensor.detach().cpu().clone()
        for name, tensor in model.state_dict().items()
        if "lora_a" in name or "lora_b" in name
    }


def load_lora_state_dict(model: nn.Module, state: dict[str, torch.Tensor]) -> None:
    """Load adapter tensors into a model that already has LoRA layers attached."""
    model = getattr(model, "_orig_mod", model)
    missing = model.load_state_dict(state, strict=False)
    unexpected = [k for k in missing.unexpected_keys]
    if unexpected:
        raise KeyError(f"adapter file contains unknown keys: {unexpected[:5]}")


def merge_lora(model: nn.Module) -> nn.Module:
    """Fold every adapter into its frozen weight, in place."""
    model = getattr(model, "_orig_mod", model)
    for module in model.modules():
        if isinstance(module, LoRALinear):
            module.merge()
    return model


def count_parameters(model: nn.Module) -> tuple[int, int]:
    """(trainable, total)."""
    model = getattr(model, "_orig_mod", model)
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return trainable, total


def print_trainable_parameters(model: nn.Module) -> None:
    """Report how much of the model is actually being trained, and what that saves."""
    trainable, total = count_parameters(model)
    adapters = [(n, m) for n, m in getattr(model, "_orig_mod", model).named_modules()
                if isinstance(m, LoRALinear)]

    print(f"trainable {trainable:,} of {total:,} parameters ({trainable / total:.2%})")
    print(f"{len(adapters)} LoRA layers attached")
    if adapters:
        rank = adapters[0][1].rank
        print(f"  rank {rank}, alpha {adapters[0][1].alpha}, "
              f"scaling {adapters[0][1].scaling:g}")
        for name, module in adapters[:4]:
            print(f"  {name:<38} {module.base.in_features:>5} -> {module.base.out_features}")
        if len(adapters) > 4:
            print(f"  ... and {len(adapters) - 4} more")

    # 4 bytes per parameter, and AdamW keeps two moments per trainable parameter.
    print(f"\noptimiser state: {trainable * 8 / 1e6:.1f} MB "
          f"(AdamW keeps 2 moments per trainable parameter)")
    print(f"  full fine-tuning would need {total * 8 / 1e6:.0f} MB for the same thing")
    print(f"adapter checkpoint: ~{trainable * 4 / 1e3:.0f} KB, "
          f"against {total * 4 / 1e6:.0f} MB for the full model")

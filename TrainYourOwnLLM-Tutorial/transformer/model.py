"""A small GPT-style (decoder-only) transformer.

Based on Andrej Karpathy's https://github.com/karpathy/ng-video-lecture/blob/master/gpt.py,
with two deliberate differences, both explained and verified in
`Notebooks/3_TransformerModel.ipynb`:

1. Attention is *fused*. The lecture version runs each head as a separate module in a
   Python loop and writes the softmax/masking by hand, which is the clearest way to
   learn the mechanics. This file computes all heads in one batched projection and
   defers to `F.scaled_dot_product_attention`.

   It is the same function: identical parameter count, outputs agreeing to float32
   noise (~2e-07). It is also faster, though modestly -- measured 1.1-1.6x for a
   forward+backward pass at batch 16, context 256, on CPU and Apple MPS. On CUDA the
   gap is generally larger because a true flash-attention kernel is selected, but that
   is not measured here. The reliable win is structural rather than arithmetic:
   `is_causal=True` needs no mask buffer, where the loop version stores a
   (block_size, block_size) mask *per head* -- 6 layers x 6 heads x 256 x 256 x 4 bytes
   = 9.4 MB of identical constants.

2. Nothing reads a global. Every hyperparameter is a constructor argument, so a model
   cannot silently disagree with the notebook cell that built it.
"""

from __future__ import annotations

from typing import Optional

import torch
import torch.nn as nn
from torch.nn import functional as F


class CausalSelfAttention(nn.Module):
    """Multi-head self-attention where each position may only look at itself and the past.

    All `n_head` heads are computed in a single batched projection rather than in a loop:
    `qkv` produces query, key and value for every head at once, and the result is reshaped
    to (batch, head, time, head_size) so the attention math runs on all heads in parallel.

    The causality comes from `is_causal=True` rather than an explicit mask buffer. The
    loop version stores a (block_size, block_size) lower-triangular mask *per head* -- 36
    identical copies for a 6-layer, 6-head model, about 9.4 MB of duplicated constants.

    It also removes the block_size ceiling from attention itself: the loop version can
    never be run on a sequence longer than the mask it allocated at construction. Here
    the only limit is the position embedding table.
    """

    def __init__(self, n_embd: int, n_head: int, dropout: float) -> None:
        super().__init__()
        if n_embd % n_head != 0:
            raise ValueError(f"n_embd ({n_embd}) must be divisible by n_head ({n_head})")

        self.n_head = n_head
        self.head_size = n_embd // n_head
        self.dropout = dropout

        # One projection producing query, key and value for every head at once.
        self.qkv = nn.Linear(n_embd, 3 * n_embd, bias=False)
        # Mixes the heads' outputs back together.
        self.projection = nn.Linear(n_embd, n_embd)
        self.residual_dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, C = x.shape

        # (B, T, 3C) -> three (B, T, C) tensors -> each (B, n_head, T, head_size)
        q, k, v = self.qkv(x).split(C, dim=2)
        q = q.view(B, T, self.n_head, self.head_size).transpose(1, 2)
        k = k.view(B, T, self.n_head, self.head_size).transpose(1, 2)
        v = v.view(B, T, self.n_head, self.head_size).transpose(1, 2)

        # Scaling by 1/sqrt(head_size) and the causal mask are both handled internally.
        # dropout_p must be 0 outside training, or evaluation becomes non-deterministic.
        y = F.scaled_dot_product_attention(
            q, k, v,
            dropout_p=self.dropout if self.training else 0.0,
            is_causal=True,
        )

        # (B, n_head, T, head_size) -> (B, T, C), i.e. concatenate the heads back together
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        return self.residual_dropout(self.projection(y))


class FeedForward(nn.Module):
    """Position-wise MLP. Widens to 4x, applies a non-linearity, projects back.

    Attention moves information *between* positions; this is where each position gets to
    do computation on what it gathered. The 4x expansion is the GPT convention.
    """

    def __init__(self, n_embd: int, dropout: float) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_embd, 4 * n_embd),
            nn.ReLU(),
            nn.Linear(4 * n_embd, n_embd),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class Block(nn.Module):
    """One transformer block: communication (attention) then computation (feed-forward).

    Both sub-layers are residual -- `x + sublayer(norm(x))` -- which is what lets gradients
    reach the early layers of a deep stack. Note the norm is applied *before* the sub-layer
    (pre-norm), not after; this is the modern arrangement and trains far more stably.
    """

    def __init__(self, n_embd: int, n_head: int, dropout: float) -> None:
        super().__init__()
        self.self_attention = CausalSelfAttention(n_embd, n_head, dropout)
        self.feed_forward = FeedForward(n_embd, dropout)
        self.layer_norm_1 = nn.LayerNorm(n_embd)
        self.layer_norm_2 = nn.LayerNorm(n_embd)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.self_attention(self.layer_norm_1(x))
        x = x + self.feed_forward(self.layer_norm_2(x))
        return x


class GPTLanguageModel(nn.Module):
    """Decoder-only transformer language model.

    Args:
        vocab_size: number of embedding rows. Must be one past the largest id the
            tokenizer can emit -- see `get_vocab_size` in notebook 3. Too small and
            training crashes on an out-of-range id; too large and the model can sample
            an id the tokenizer cannot decode.
        block_size: maximum context length. Also the number of position embeddings, so
            the model physically cannot attend further back than this.
        n_embd: width of the residual stream.
        n_head: number of attention heads. Must divide n_embd.
        n_layer: number of transformer blocks.
        dropout: dropout probability, applied in attention, the MLP and the residuals.
        device: optional convenience -- if given, the model is moved there on construction.
            The forward pass does not depend on it; tensors follow the input's device.
    """

    def __init__(
        self,
        vocab_size: int,
        block_size: int,
        n_embd: int,
        n_head: int,
        n_layer: int,
        dropout: float,
        device: Optional[str | torch.device] = None,
    ) -> None:
        super().__init__()
        self.block_size = block_size
        self.vocab_size = vocab_size

        self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
        self.position_embedding_table = nn.Embedding(block_size, n_embd)
        self.blocks = nn.Sequential(
            *[Block(n_embd, n_head, dropout) for _ in range(n_layer)]
        )
        self.final_layer_norm = nn.LayerNorm(n_embd)
        self.final_linear_layer = nn.Linear(n_embd, vocab_size)

        self.apply(self._init_weights)
        if device is not None:
            self.to(device)

    @staticmethod
    def _init_weights(module: nn.Module) -> None:
        if isinstance(module, nn.Linear):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(
        self,
        input_tokens: torch.Tensor,
        targets: Optional[torch.Tensor] = None,
    ) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        """Run the model.

        Args:
            input_tokens: (batch, time) token ids.
            targets: optional (batch, time) ids to score against. Target t is the token
                that should follow input t, so the caller offsets them by one.

        Returns:
            (logits, loss). logits is (batch, time, vocab_size); loss is None when no
            targets are given.
        """
        B, T = input_tokens.shape
        if T > self.block_size:
            raise ValueError(
                f"sequence length {T} exceeds block_size {self.block_size}; "
                "there is no position embedding for it"
            )

        # Positions live on the input's device, not a captured global -- so the model
        # works wherever it was moved to, without being told twice.
        positions = torch.arange(T, device=input_tokens.device)

        x = self.token_embedding_table(input_tokens) + self.position_embedding_table(positions)
        x = self.blocks(x)
        x = self.final_layer_norm(x)
        logits = self.final_linear_layer(x)

        if targets is None:
            return logits, None

        # cross_entropy wants (N, C) against (N), so flatten batch and time together.
        loss = F.cross_entropy(logits.view(B * T, -1), targets.reshape(B * T))
        return logits, loss

    @torch.no_grad()
    def generate(
        self,
        input_tokens: torch.Tensor,
        max_new_tokens: int,
        temperature: float = 1.0,
        eos_token_id: Optional[int] = None,
    ) -> torch.Tensor:
        """Sample a continuation, one token at a time.

        Decorated with `no_grad` because sampling never needs gradients; without it every
        generated token grows an autograd graph that is built and thrown away.

        Args:
            input_tokens: (batch, time) prompt ids.
            max_new_tokens: how many tokens to append at most.
            temperature: <1 sharpens the distribution, >1 flattens it.
            eos_token_id: if given, stop as soon as every sequence has produced it. This
                is why an end-of-text marker has to be a single token id -- see notebook 2.

        Returns:
            (batch, time + generated) ids, prompt included.
        """
        was_training = self.training
        self.eval()  # dropout during sampling would add noise for no reason
        try:
            finished = torch.zeros(
                input_tokens.shape[0], dtype=torch.bool, device=input_tokens.device
            )
            for _ in range(max_new_tokens):
                # Only the last block_size tokens fit in the context window.
                cropped_input = input_tokens[:, -self.block_size:]
                logits, _ = self(cropped_input)
                logits = logits[:, -1, :] / temperature  # last step only
                probabilities = F.softmax(logits, dim=-1)
                next_token = torch.multinomial(probabilities, num_samples=1)
                input_tokens = torch.cat((input_tokens, next_token), dim=1)

                if eos_token_id is not None:
                    finished |= next_token.squeeze(1) == eos_token_id
                    if bool(finished.all()):
                        break
            return input_tokens
        finally:
            self.train(was_training)

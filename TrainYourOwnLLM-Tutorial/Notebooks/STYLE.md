# Notebook style guide

Conventions used in `1_DataCleaning` and `2_BytePairEncoding`. Read this instead of reading
those notebooks. Every rule here exists because breaking it cost us something real — the
failures are named so you can tell when a rule stops applying.

These notebooks are a **tutorial and a pipeline at the same time**. Each one is read by a
learner top to bottom, and its output file is loaded by the next one. Both jobs matter.

---

## 1. Notebook shape

Cells run in a fixed order. Do not make the reader jump around.

```
markdown   what this notebook produces, and the stages it goes through (a table)
markdown   the rules / the concept, explained before any code
code       imports + module-level constants (compiled regexes, patterns, sizes)
markdown   why the next function is written the way it is
code       helpers, defined before whatever uses them
code       the main function
markdown   what could silently go wrong here
code       CHECK CELL - assertions (see §4)
code       run it on the real data, print a summary
code       write the artifact, print the resolved path
code       reload the artifact and assert it survived the round-trip
```

- **One idea per cell.** A cell that both defines a parser and runs it on 7 files is two cells.
- **Every code cell gets a markdown cell above it** — unless it is a trivial continuation.
- **No empty cells, no comment-only code cells.** Instructions belong in markdown.
  Notebook 2 had a code cell containing nothing but `# pip install ...` comments; that is
  markdown wearing a costume.
- **Constants are `UPPER_CASE` and live in one cell**, near the top. If a regex is compiled
  inside a function that runs per line, it is in the wrong place.

---

## 2. Prose

The markdown is the teaching. Aim it at the trap, not the syntax.

- **Explain why, not what.** "`str.translate` applies the whole table in one C-level pass"
  beats "this replaces characters."
- **Name the failure concretely.** Not "this can cause issues" but "the model samples id
  1031, no vocab entry covers it, and generation dies with `KeyError` inside `decode()`."
- **Prose must match the output.** Notebook 2 claimed "more than 618K tokens" directly above
  a cell printing `57847`. If you change a number, re-read the markdown around it.
- **State costs honestly.** `RegexTokenizer` made training *slower* (10.8s → 17.1s) while
  making encoding 52x faster. Say both. A guide that only lists wins is not trustworthy.
- **Use tables for rule lists and comparisons.** Ten filtering rules as prose is unreadable;
  as a table it is scannable.
- **Comments carry the reason.** `# max() rather than "the last key", which would depend on
  dict ordering` is worth its line. `# increment counter` is not.

---

## 3. Code

### Always

| Do | Instead of | Because |
| --- | --- | --- |
| `Path(p).read_text(encoding="utf-8")` | `open(...).read()`, `os.path` | one style; `encoding` is never implicit |
| Compile regexes once at module level | `re.search(pattern, line)` in a loop | the compile cost is paid per call otherwise |
| `re.VERBOSE` with a comment per branch | one long unreadable pattern | you will need to edit it later |
| `" ".join(generator)` | `s += x` in a loop | `+=` re-copies the string each time: quadratic |
| `str.translate(table)` | chained `.replace().replace()` | one pass instead of N |
| Build lists, then one `pd.DataFrame(...)` | appending rows | pandas allocates each column once |
| `sorted(dir.glob("*.txt"))` | bare `glob` | glob order is filesystem-dependent; output must be reproducible |
| `max(d)` | `list(d.keys())[-1]` | the second silently depends on insertion order |
| Reuse a computed result across cells | recomputing it | notebook 2 encoded the corpus twice for no reason |

### Never call a vectorized function per row

This was 91% of notebook 1's runtime:

```python
# NO - pd.to_datetime is vectorized; calling it per element pays the setup cost N times
for ts in df["timestamp"]:
    timestamps.append(pd.to_datetime(ts, format="mixed", errors="coerce"))

# YES
df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", errors="coerce")
```

That loop was 0.277s of the notebook's 0.303s total. Vectorizing it alone brings the
notebook to ~0.13s; the rest of the rules here take it the remaining way to 0.036s.

### Prefer an exact format, with a fallback for the remainder

`format="mixed"` re-sniffs every value. Infer the format once, apply it vectorized, and
re-parse only the rows that came back `NaT`:

```python
parsed = pd.to_datetime(stamps, format=fmt, errors="coerce")
missing = parsed.isna()
if missing.any():
    parsed[missing] = pd.to_datetime(stamps[missing], format="mixed",
                                     dayfirst=day_first, errors="coerce")
```

Fast in the common case, still correct on mixed input. ~13x on our data.

### `errors="coerce"` already swallows failures

So a `try/except` around it is dead code. Notebook 1 had one. If you want to know about
failures, count the `NaT`s afterwards and print that.

### Locale-ambiguous input must be inferred, not assumed

`01/03/2026` is 1 March or 3 January depending on the exporting device. pandas silently
defaults to month-first, so a day-first file parses as a *mix* of correct and swapped dates
— no exception, no `NaT`. **2305 of 5419 timestamps were wrong** and nothing complained.
Infer from the data (find a component > 12) rather than trusting a default.

### Read the library before trusting it

`BasicTokenizer`'s docstring says it "does not handle any special tokens." We assigned to
`.special_tokens` anyway; `encode()` ignored them and `decode()` raised `KeyError`. Two
minutes in the source would have saved that.

---

## 4. The check cell

**This is the highest-value habit in these notebooks.** It found every bug we fixed.

A check cell runs assertions on a small hand-written sample *before* anything downstream
depends on the result. It takes milliseconds. Put one after any function that transforms
data.

```python
assert actual == EXPECTED, "\n".join(
    ["cleaning rules did not behave as documented:"]
    + [f"  expected {row}" for row in EXPECTED]
    + [f"  actual   {row}" for row in actual]
)
```

Rules:

- **Cover the rules your real data does not.** Our chats exercise only 4 of 10 cleaning
  rules. The `null` rule had been broken from the start — `line.split(" ")[-1]` compared
  against `readlines()` output, which keeps the trailing `\n`, so it never matched anything.
  Six untested rules is six places a bug can sit forever.
- **Assert the round-trip.** `decode(encode(x)) == x` on the real corpus, not a toy string.
- **Assert the full range, not a sample.** `for idx in range(vocab_size): decode([idx])` is
  what catches an id the model can emit but the vocabulary cannot cover.
- **Assert across the save/load boundary.** A tokenizer that works in memory but not after
  a disk round-trip breaks two notebooks later, where the cause is invisible.
- **Failure messages print expected vs actual.** A bare `assert x == y` tells you nothing at
  3am.

---

## 5. Artifacts and the contract between notebooks

Each notebook writes files the next one loads. Treat that as an interface.

- **Print the resolved path and size** after writing. `print(f"wrote {p.resolve()} ({n:,} chars)")`
- **`mkdir(parents=True, exist_ok=True)`** before writing.
- **Sort anything you iterate** so the artifact is byte-identical run to run. The tokenizer
  is trained on this text; non-determinism upstream means an unreproducible model.
- **Derive sizes, never count them twice.** `len(vocab) + len(special_tokens)` returns 1029
  on a fresh tokenizer and 1034 on a reloaded one, because `load()` folds the specials into
  `.vocab`. Use `max(largest_id) + 1`, which is correct in both states.
- **Changing an artifact means re-running every notebook downstream.** Say so in the summary.

---

## 6. Scope

- **Fix the bug; do not add features.** Notebook 1 documents 10 cleaning rules. One was
  broken, so it got fixed. WhatsApp's `This message was deleted` is a plausible 11th rule —
  it was *reported*, not added.
- **Prove equivalence when you refactor.** Diff old vs new output cell by cell and account
  for every difference. Notebook 1's rewrite produced exactly two classes of change: 2305
  day↔month corrections and 84 whitespace fixes. Zero unexplained.
- **Ask before invalidating saved artifacts.** Switching tokenizers meant retraining and
  editing two downstream notebooks. That is the author's call, not a refactor.
- **Do not re-run expensive notebooks to "verify".** Notebooks 3 and 4 train transformers.
  Patch the source, run the affected cells in isolation, and say clearly that the notebooks
  need a re-run.

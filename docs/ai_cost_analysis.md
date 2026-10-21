# CashProof — AI Parsing Cost Analysis

Scenario: **12 bank statement PDFs × 30 pages each**, parsed by
`analyzer/parsers/pdf_parser.py` (`_parse_via_openai`, Azure OpenAI client).

## 1. System Overview

Two AI tasks per PDF:

| Task | Description | Calls per file |
|---|---|---|
| Metadata extraction | Bank name, account number, dates, balances | 1 |
| Transaction extraction | Chunked at 5 pages/call (`PDF_PAGE_CHUNK_SIZE`) | ceil(30/5) = 6 |

- Text-mode: 7 calls/file → 84 calls for 12 files
- Vision-mode (scanned): 31 calls/file → 372 calls for 12 files

## 2. Model Pricing (per 1M tokens)

| Model | Input | Output | Cached input |
|---|---|---|---|
| GPT-5.5 | $5.00 | $30.00 | ~$1.25 |
| GPT-4.1 | $2.00 | $8.00 | ~$0.50 |
| GPT-4.1 mini | $0.40 | $1.60 | ~$0.10 |

> Verify against the current OpenAI / Azure OpenAI pricing page before relying on these.

## 3. Token Estimates (per 30-page file)

| Mode | Calls | Input tokens | Output tokens |
|---|---|---|---|
| Text PDF | 7 | 32,480 | 7,400 |
| Vision PDF (~1,600 tokens/page @150 DPI) | 31 | ~64,000 | ~36,200 |

## 4. Per-File Cost

| Mode | GPT-5.5 | GPT-4.1 | GPT-4.1 mini |
|---|---|---|---|
| Text | $0.384 | $0.124 | $0.025 |
| Vision | $1.406 | $0.418 | $0.084 |

## 5. Total for 12 Files (base)

| Scenario | GPT-5.5 | GPT-4.1 | GPT-4.1 mini |
|---|---|---|---|
| All text | $4.61 | $1.49 | $0.30 |
| All vision | $16.87 | $5.01 | $1.00 |
| Mixed 50/50 | $10.74 | $3.25 | $0.65 |

## 6. Final Cost with +30% Overhead

Overhead: retries (+15%), suspicious-page reprocessing (+10%), chunk-truncation fallback (+5%).
INR at ₹84/USD.

| Scenario | GPT-5.5 | GPT-4.1 | GPT-4.1 mini |
|---|---|---|---|
| All text | $5.99 (₹503) | $1.94 (₹163) | $0.39 (₹33) |
| All vision | $21.93 (₹1,842) | $6.51 (₹547) | $1.30 (₹109) |
| Mixed | $13.96 (₹1,173) | $4.23 (₹355) | $0.85 (₹71) |

## 7. Savings vs GPT-5.5

| Scenario | GPT-4.1 | GPT-4.1 mini |
|---|---|---|
| All text | ~68% | ~93.5% |
| All vision | ~70% | ~94% |
| Mixed | ~70% | ~94% |

## 8. Assumptions & Caveats

- Same token counts used for all models; only pricing changes.
- GPT-5.5 may bill hidden reasoning tokens, so real savings with GPT-4.1 could be larger.
- Image token counts may differ per model; confirm with real usage data.
- The code uses `AzureOpenAI` (`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `OPENAI_MODEL`
  setting). Azure deployment names and prices may differ from OpenAI list prices.
- The default model is `gpt-5.5`; change `OPENAI_MODEL` (and the Azure deployment) to switch.
- Validate extraction accuracy on sample statements, especially scanned PDFs with mini.
- GPT-5.5 retirement (Oct 14, 2026) per the original analysis — migration recommended.

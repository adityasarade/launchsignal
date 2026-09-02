# Zero-spend operating rule

LaunchSignal runs at $0. This file records the rule and how the code enforces it.

LaunchSignal is being built under a strict $0 cap unless the account holder explicitly changes that instruction.

- Tinyfish: use only free Search/Fetch. Never reload the wallet, use paid Browser/Agent, or place a payment method.
- X: do not create credits, buy credits, or use a billable token. The code path stays unconfigured; public Tinyfish X discovery and fixtures remain available for development.
- Slack: create/install only a free custom app in a workspace the account holder controls; do not enter payment details or select a paid plan.
- Hosting: do not deploy to a service that asks for payment, credit-card details, or a paid plan. Local persistence is the current development target.

Any screen requesting money, credits, a card, or a wallet reload is a stop condition.

## How the code enforces this

- There is no X API client in the codebase. `X_BEARER_TOKEN` is not read
  anywhere, so no billable Post-read path can be reached even by accident.
- Public search is used only through its free Search endpoint. Paid
  Browser/Agent/Fetch products are never called.
- Every HTTP call goes through `http.request`, which has a hard attempt cap and
  bounded backoff, so a retry loop cannot run away into a metered endpoint.
- Slack's `chat.postMessage` and `auth.test` are free on every plan, including
  the free tier, and the app needs exactly one scope (`chat:write`).
- `SLACK_DRY_RUN` defaults to `true`, so a fresh clone cannot post anywhere
  until someone opts in explicitly.

Any screen asking for money, credits, a card, or a wallet reload is a stop
condition.

# Atlas documentation

| Read this | To learn |
|---|---|
| [01 Product overview](01-product-overview.md) | What Atlas is, who it is for, and what it deliberately does not do |
| [02 Architecture](02-architecture.md) | The research pipeline, retrieval, routing, data model, security and deployment, with diagrams |
| [03 Evaluation](03-evaluation.md) | How models and answers are measured: AtlasBench, the grounding suite, scoring and statistics |
| [04 Testing and results](04-testing-and-results.md) | What was run, the actual results, and what was not verified |
| [05 Development history](05-development-history.md) | Milestones and the hardening work, in order |
| [06 Runbook](06-runbook.md) | Operating a deployment: health checks, limits, admin tasks, incidents |
| [07 Deployment guide](07-deployment.md) | Render, Docker, Docker Compose and Hugging Face Spaces, step by step |
| [08 API reference](08-api-reference.md) | Every route with access level and rate limit |
| [09 Configuration reference](09-configuration.md) | Every environment variable |
| [Changelog](CHANGELOG.md) | What changed |
| [Project management pack](project/README.md) | Charter, requirements (PRD), roadmap and milestones, backlog, risk register, QA report, decision log and closure report |

**First time here?** Read the [project README](../README.md), then 01 and 02. **Deploying?** 07, then 09 and 06. **Integrating?** 08. **Reviewing quality?** 03 and 04. **Reviewing how the project was run?** The [project management pack](project/README.md).

Component notes live next to the code: [`backend/bench/README.md`](../backend/bench/README.md) (AtlasBench), [`backend/routing/README.md`](../backend/routing/README.md) (Cost Autopilot) and [`backend/ops/README.md`](../backend/ops/README.md) (limits, spend caps, admin).

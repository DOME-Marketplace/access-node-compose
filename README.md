# Access Node Docker Compose: component and dependencies description

This README serves as the **Formal Service Registry** for our project. To ensure all 20+ services across multiple companies remain integrated and documented, we use a metadata-as-code approach.

Every service defined in this `docker-compose.yml` **must** include specific labels. These labels are compatible with the [Backstage.io](https://www.google.com/search?q=https://backstage.io) standard, allowing us to automatically generate dependency graphs and system health reports.

---

## 🛠 Service Metadata Standard

All architectural metadata must be prefixed with `bs.` (Backstage). Our internal Go tooling parses these labels to validate the architecture and render our [Service Graph](https://www.google.com/search?q=%23service-graph).

### Required Labels

| Label Key | Type | Description | Example |
| --- | --- | --- | --- |
| `bs.spec.owner` | String | The company/team responsible for this service. | `company-a` |
| `bs.spec.type` | Enum | The architectural role: `service`, `database`, `proxy`. | `service` |
| `bs.spec.lifecycle` | Enum | Current maturity: `experimental`, `production`, `deprecated`. | `production` |
| `bs.spec.dependsOn` | List | Comma-separated list of services this component calls. | `component:auth-api,component:order-db` |

### Optional (But Recommended) Labels

| Label Key | Description | Example |
| --- | --- | --- |
| `bs.metadata.description` | Short summary of the service's purpose. | "Processes credit card payments" |
| `bs.metadata.annotations.contact` | Direct Slack channel or email for the on-call dev. | `slack:#team-payments-dev` |
| `bs.metadata.annotations.versions` | Version constraints for dependencies (CSV). | `auth-api:>=2.1,order-db:any` |

---

## 📝 Implementation Example

When adding or updating your service, please follow this template:

```yaml
services:
  payment-gateway:
    image: company-a/payment-gw:v2.4.0
    # ... standard docker configs (ports, volumes, etc) ...
    labels:
      # Ownership & Lifecycle
      - "bs.spec.owner=Company-A"
      - "bs.spec.type=service"
      - "bs.spec.lifecycle=production"
      
      # Description & Contact
      - "bs.metadata.description=Handles PCI-compliant gateway handshakes"
      - "bs.metadata.annotations.contact=dev-ops@companya.com"
      
      # Downstream Dependencies (The Tree)
      - "bs.spec.dependsOn=component:auth-service,component:payment-db"
      
      # Version Constraints (Custom)
      - "bs.metadata.annotations.versions=auth-service:^1.2.0"

```

---

## ⚖️ Rules of Engagement

1. **Unique Naming:** The service name in `docker-compose.yml` (e.g., `payment-gateway`) is the "Entity Name." Use this exact name in `bs.spec.dependsOn` prefixed with `component:`.
2. **No Comments for Metadata:** Do not use YAML comments (`#`) for owner or dependency info. Our Go parser ignores comments; it only reads **labels**.
3. **Sync Required:** If you add a new API call from Service A to Service B, you **must** update the `bs.spec.dependsOn` label in the same PR.
4. **CI Validation:** Our CI pipeline runs a Go-based linter. If a service is missing the `bs.spec.owner` or `bs.spec.type` labels, the build will fail.

---

## 📊 Visualizing the Graph

To see the current dependency tree, run the local generator tool (requires Go):

```bash
go run ./tools/graph-gen --input docker-compose.yml --output graph.mmd

```

*You can paste the contents of `graph.mmd` into the [Mermaid Live Editor](https://www.google.com/search?q=https://mermaid.live/) to see the full architecture.*

---

**Would you like me to provide the Go code for the "CI Linter" that validates these labels before a PR can be merged?**
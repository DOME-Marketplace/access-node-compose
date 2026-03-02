# Access Node Docker Compose: component and dependencies description

This README defines the formal requirements for the description of the Docker Compose file which implements a DOME Access Node using Docker Compose. The objective of this repo is to facilitate an Access Node implementation which is easy to understand, install and operate for those entities external to DOME which wish to use this form of Access Node.

To ensure all services remain integrated and documented, we use a metadata-as-code approach, including information about the services and their dependencies directly in the `compose.yaml` file.

Every service defined in this `compose.yaml` **must** include specific labels. These labels allow the generation of documentation and dependency graphs, which can be used to better implement safe and controlled deployments.

These labels are alligned with the [Backstage.io](https://backstage.io) standard, allowing us to use in the future that tool or any similar one to manage the software catalog in DOME.

---

## Implementation Example

When adding or updating your service, please follow this template:

```yaml
services:
  desmos:
    image: in2workspace/in2-desmos-api:v2.0.3
    # ... standard docker configs (ports, volumes, etc) ...
    labels:
      # Description
      - "dome.name=desmos"
      - "dome.description=Manages replication of data across nodes"

      # Ownership & Lifecycle
      - "dome.owner=Altia"
      - "dome.type=service"
      - "dome.lifecycle=sandbox"
            
      # Downstream Dependencies and version constraints
      - "dome.dependsOn=component:scorpio,component:auth-service"
      - "dome.versions=auth-service:>=2.1,scorpio:any"

```


## Service Metadata Standard

All architectural metadata must be prefixed with `dome.`, acting as our namespace. We will develop or use simple tooling to parse these labels to validate the architecture and render our **Service Graph**.

### Required Labels

| Label Key | Type | Description | Example |
| --- | --- | --- | --- |
| `dome.name` | String | The unique name of the service, corresponding to the service name in `compose.yml`. | `desmos` |
| `dome.description` | String | Short summary of the service's purpose. | `"Manages replication of data across nodes` |
| `dome.owner` | String | The company/team responsible for this service. | `Altia` |
| `dome.type` | Enum | The architectural role: `service`, `database`, `proxy`. | `service` |
| `dome.lifecycle` | Enum | Current maturity: `development`, `sandbox`, `preproduction`, `production`. | `production` |
| `dome.dependsOn` | List | Comma-separated list of services this component calls at runtime. | `component:scorpio,component:auth-service` |
| `dome.versions` | String | Version constraints for dependencies (CSV). | `auth-service:>=2.1,scorpio:any` |

---

## Additional information

1. **Unique Naming:** The service name in `compose.yaml` (e.g., `desmos`) is the "Entity Name." Use this exact name in `dome.dependsOn` prefixed with `component:`.
2. **No Comments for Metadata:** Do not use YAML comments (`#`) for owner or dependency info.
3. **Sync Required:** If you add a new API call from Service A to Service B, you **must** update the `dome.dependsOn` label for Service A in the same PR.
4. **CI Validation:** Our (WIP) CI pipeline runs a checker. If a service is missing the `dome.owner` or `dome.type` labels, the build will fail.

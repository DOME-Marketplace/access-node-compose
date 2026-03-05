import sys
yaml_path = sys.argv[1]
import yaml
with open(yaml_path) as f:
    data = yaml.safe_load(f)
services = data.get('services', {})
missing = []
for name, svc in services.items():
    labels = svc.get('labels', {})
    if not labels or not all(k in labels for k in ['dome.name', 'dome.description', 'dome.owner', 'dome.type', 'dome.lifecycle', 'dome.versions']):
        missing.append(name)
if missing:
    print(f"Missing required labels in services: {', '.join(missing)}")
    sys.exit(1)
print("All services have required metadata labels.")

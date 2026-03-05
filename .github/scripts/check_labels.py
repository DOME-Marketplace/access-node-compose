import sys
yaml_path = sys.argv[1]
import yaml
with open(yaml_path) as f:
    data = yaml.safe_load(f)
services = data.get('services', {})
required_keys = ['dome.name', 'dome.description', 'dome.owner', 'dome.type', 'dome.lifecycle', 'dome.versions']
missing = {}
for name, svc in services.items():
    labels = svc.get('labels', [])
    # Labels can be a list of "key=value" strings or a dict
    if isinstance(labels, list):
        label_keys = {lbl.split('=')[0] for lbl in labels if '=' in lbl}
    else:
        label_keys = set(labels.keys()) if labels else set()
    missing_keys = [k for k in required_keys if k not in label_keys]
    if missing_keys:
        missing[name] = missing_keys
if missing:
    print("Missing required labels:")
    for svc_name, keys in missing.items():
        print(f"  {svc_name}: {', '.join(keys)}")
    sys.exit(1)
print("All services have required metadata labels.")

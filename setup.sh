#!/bin/bash
# setup.sh —init configuration files from examples

for template in $(find . -name "*.example" -not -path "./.git/*"); do
    target="${template%.example}"
    if [ ! -f "$target" ]; then
        cp "$template" "$target"
        echo "Created: $target"
    else
        echo "· Already exists: $target (skipped)"
    fi
done

echo ""
echo "Modify the .env files with your values before starting the services."
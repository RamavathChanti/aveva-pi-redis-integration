# Apply this update to the existing GitHub repository

This package is a full replacement copy of the demo source. To apply it to the existing repository:

```bash
# 1. Back up or commit the current working tree
cd ~/aveva-pi-redis-integration
git status
git add .
git commit -m "Checkpoint before custom web demo"   # only when appropriate

# 2. Copy the files from this package over the repository
# Replace <downloaded-folder> with the extracted package location.
rsync -av --exclude='.git' <downloaded-folder>/ ./

# 3. Review and test
git diff
docker compose down
docker compose up --build
```

Open the customer application at:

```text
http://localhost:3000
```

After validation:

```bash
git add .
git commit -m "Replace RedisInsight demo with customer web application"
git push
```

The update does not require RedisInsight. Redis remains available on port `6379`, while the new dashboard is exposed on port `3000`.

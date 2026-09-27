- Fix sessions indexed while the local Ollama daemon was down (eg. its model
  cache living on an external drive) never getting semantic embeddings: those
  jobs are now queued and a background backfill catches up any session
  missing one.

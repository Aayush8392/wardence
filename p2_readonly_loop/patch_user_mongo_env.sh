#!/usr/bin/env bash
# Real bug found 2026-08-23 during the wardence-prod arm64 deployment:
# the stock upstream complete-demo.yaml sets user's Mongo-host env var
# with the literal name "mongo" (a Docker-Compose-link-style convention
# the real published weaveworksdemos/user:0.4.7 image apparently reads
# directly) -- but the actual user service SOURCE (confirmed at the
# exact deployed tag, 0.4.7) only reads MONGO_HOST via
# os.Getenv("MONGO_HOST") in db/mongodb/mongodb.go. Our from-source
# arm64 rebuild (deploy/rebuild_arm64_user.sh) therefore never saw the
# real host value and fell back to the Dockerfile's own baked-in
# placeholder default (mytestdb:27017), producing an endless
# "no reachable servers" loop despite user-db being genuinely healthy.
#
# This is a manifest-level fix (not image-level), so it's needed every
# time complete-demo.yaml is freshly applied from upstream -- rerun
# after any full redeploy, same as the other README section 2 patches.
#
# Usage: bash patch_user_mongo_env.sh

set -euo pipefail

NAMESPACE="sock-shop"
DEPLOYMENT="user"

echo "Patching $DEPLOYMENT: renaming env var 'mongo' -> 'MONGO_HOST' (real name the app source reads)..."
kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/env/0/name","value":"MONGO_HOST"}]'

echo "Waiting for rollout..."
kubectl rollout status deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout=200s

echo
echo "Patching $DEPLOYMENT: imagePullPolicy -> Always (real bug found"
echo "2026-08-24: IfNotPresent let the node reuse a stale cached image"
echo "under the same tag after an arm64 rebuild -- see"
echo "wardence_buildlog.md's network-latency validation session, the"
echo "user Href-struct fix)..."
kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"user","imagePullPolicy":"Always"}]}}}}'
kubectl rollout status deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout=200s

echo
echo "Done. Confirm with:"
echo "  kubectl get deployment user -n $NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].env}{\"\n\"}'"
echo "  (expect: [{\"name\":\"MONGO_HOST\",\"value\":\"user-db:27017\"}])"

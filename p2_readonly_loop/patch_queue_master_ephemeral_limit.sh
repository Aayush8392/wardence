#!/bin/sh
# One-time setup for the disk-full fault class. queue-master is the
# target (not payment -- payment's root filesystem is readOnlyRootFilesystem:
# true with only tmpfs/Memory-backed mounts, so nothing writable to it
# would ever count as real ephemeral-storage; queue-master has a
# writable, disk-backed /tmp confirmed via kubectl exec). No service in
# Sock Shop had an ephemeral-storage limit set until now. Strategic
# merge patch only adds this field; existing cpu/memory limits and
# everything else on the deployment are left untouched.
kubectl patch deployment queue-master -n sock-shop --type=strategic -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "queue-master",
            "resources": {
              "limits": {
                "ephemeral-storage": "100Mi"
              }
            }
          }
        ]
      }
    }
  }
}
'

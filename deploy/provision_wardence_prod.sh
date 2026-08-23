#!/usr/bin/env bash
# Real provisioning script for the Oracle A1 `wardence-prod` host
# (Ubuntu 24.04 aarch64, confirmed live 2026-08-23 -- see
# wardence_buildlog.md's matching session for the SSH/network setup that
# got the host to this point).
#
# Run this ON THE ORACLE HOST itself (via SSH), not on the Windows dev
# machine -- Claude does not and cannot execute this.
#
#   ssh -i ~/.ssh/wardence-prod.key ubuntu@141.148.214.51
#   git clone https://github.com/Aayush8392/wardence.git
#   cd wardence
#   chmod +x deploy/provision_wardence_prod.sh
#   sudo ./deploy/provision_wardence_prod.sh
#
# Idempotent-ish: safe to re-run if a step fails partway (k3s/apt installs
# no-op if already present; directory/venv steps use -p/existence checks).
# NOT a full deploy -- this stops right before deploy/README.md's section 1
# (base application + observability stack). Run this first, then follow
# the README from section 1 onward.
#
# What this script does NOT do (deliberately, per the runbook):
#   - Does not install Docker on this host -- k3s ships its own containerd,
#     which is all that's needed to pull and run the already-built arm64
#     images from GHCR. Docker was only needed on the WSL2 build machine.
#   - Does not fill in real secrets (.env files, JWT secret, SA token) --
#     those are either copied manually from the dev machine or generated
#     fresh per the README's section 5/6. This script only creates the
#     directories/permissions they'll need to land in.
#   - Does not touch CORS_ORIGINS/PROMETHEUS_URL in the systemd unit --
#     those need the real Vercel domain and the Prometheus NodePort, which
#     don't exist until later steps in the README.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./deploy/provision_wardence_prod.sh" >&2
  exit 1
fi

REAL_USER="${SUDO_USER:-ubuntu}"
REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="${REPO_PATH}/p3_trust_action/.venv"

echo "== Provisioning wardence-prod =="
echo "   Running as real user: ${REAL_USER}"
echo "   Repo path:            ${REPO_PATH}"
echo "   Venv path:            ${VENV_PATH}"
echo

# ---------------------------------------------------------------------
# 1. Base packages
# ---------------------------------------------------------------------
echo "-- Installing base packages (curl, git, python3-venv, jq) --"
apt-get update -y
apt-get install -y curl git python3-venv python3-pip jq

# ---------------------------------------------------------------------
# 2. k3s
# ---------------------------------------------------------------------
if command -v k3s >/dev/null 2>&1; then
  echo "-- k3s already installed, skipping install --"
else
  echo "-- Installing k3s --"
  curl -sfL https://get.k3s.io | sh -
fi

echo "-- Waiting for k3s node to be Ready --"
for i in $(seq 1 30); do
  if k3s kubectl get nodes 2>/dev/null | grep -q " Ready"; then
    echo "   node Ready"
    break
  fi
  sleep 2
done
k3s kubectl get nodes

# ---------------------------------------------------------------------
# 3. kubectl config for the real (non-root) user
# ---------------------------------------------------------------------
echo "-- Setting up ~/.kube/config for ${REAL_USER} --"
REAL_HOME=$(getent passwd "${REAL_USER}" | cut -d: -f6)
mkdir -p "${REAL_HOME}/.kube"
cp /etc/rancher/k3s/k3s.yaml "${REAL_HOME}/.kube/config"
chown "${REAL_USER}:${REAL_USER}" "${REAL_HOME}/.kube/config"
chmod 600 "${REAL_HOME}/.kube/config"

# Real gotcha found live (2026-08-23): `k3s kubectl` (what the symlink
# below actually runs) does NOT follow the standard kubectl convention of
# defaulting to ~/.kube/config -- it only checks $KUBECONFIG, defaulting
# to the root-only /etc/rancher/k3s/k3s.yaml if unset, which a non-root
# user can't read ("permission denied" even with a valid ~/.kube/config
# sitting right there). Must export KUBECONFIG explicitly.
if ! grep -q "^export KUBECONFIG=" "${REAL_HOME}/.bashrc" 2>/dev/null; then
  echo 'export KUBECONFIG=~/.kube/config' >> "${REAL_HOME}/.bashrc"
  chown "${REAL_USER}:${REAL_USER}" "${REAL_HOME}/.bashrc"
fi
echo "   KUBECONFIG export added to ${REAL_HOME}/.bashrc -- start a new"
echo "   shell (or 'source ~/.bashrc') before running kubectl commands."

# k3s bundles its own kubectl; symlink so plain `kubectl` works too
if ! command -v kubectl >/dev/null 2>&1; then
  ln -s "$(command -v k3s)" /usr/local/bin/kubectl
fi

# ---------------------------------------------------------------------
# 4. GHCR pull credentials
# ---------------------------------------------------------------------
# The 7 rebuilt arm64 images (ghcr.io/aayush8392/{front-end,carts,orders,
# queue-master,shipping,catalogue,user,payment}:*-arm64) were pushed under
# a personal GHCR account -- if the packages are private (GHCR default
# unless explicitly made public), the cluster needs real pull credentials
# or every pod will sit in ImagePullBackOff. Same PAT used to push them
# (write:packages scope) works for pulling too.
echo
echo "-- GHCR pull credentials --"
read -rp "Are the 7 GHCR images public? [y/N]: " GHCR_PUBLIC
if [[ "${GHCR_PUBLIC,,}" != "y" ]]; then
  read -rp "GitHub username (for GHCR login): " GHCR_USER
  read -rsp "GitHub PAT (write:packages or read:packages scope): " GHCR_PAT
  echo
  k3s kubectl create namespace sock-shop --dry-run=client -o yaml | k3s kubectl apply -f -
  k3s kubectl create secret docker-registry ghcr-pull-secret \
    --namespace sock-shop \
    --docker-server=ghcr.io \
    --docker-username="${GHCR_USER}" \
    --docker-password="${GHCR_PAT}" \
    --dry-run=client -o yaml | k3s kubectl apply -f -
  # Attach to the namespace's default ServiceAccount so every pod that
  # doesn't specify its own imagePullSecrets still picks this one up --
  # the microservices-demo upstream manifest doesn't set imagePullSecrets
  # itself, so this is the one clean way to cover all 7 rebuilt images
  # without hand-editing the manifest.
  k3s kubectl patch serviceaccount default -n sock-shop \
    -p '{"imagePullSecrets": [{"name": "ghcr-pull-secret"}]}'
  echo "   ghcr-pull-secret created and attached to sock-shop's default SA."
else
  echo "   Skipped -- confirm the images are genuinely public before"
  echo "   deploying, or every pod will hit ImagePullBackOff."
fi

# ---------------------------------------------------------------------
# 5. Python venv for operator_api.py (pinned interpreter, per Qwen
#    review 63's warning against a bare `python3 -m venv` against
#    whatever the system default happens to be)
# ---------------------------------------------------------------------
echo
echo "-- Setting up Python venv at ${VENV_PATH} --"
PYTHON_BIN=$(command -v python3.12 || command -v python3)
echo "   Using interpreter: ${PYTHON_BIN} ($(${PYTHON_BIN} --version))"
if [[ ! -d "${VENV_PATH}" ]]; then
  sudo -u "${REAL_USER}" "${PYTHON_BIN}" -m venv "${VENV_PATH}"
fi
sudo -u "${REAL_USER}" "${VENV_PATH}/bin/pip" install --upgrade pip
sudo -u "${REAL_USER}" "${VENV_PATH}/bin/pip" install -r "${REPO_PATH}/p3_trust_action/requirements.txt"

# ---------------------------------------------------------------------
# 6. Persistent state dir for operator_api.py (WARDENCE_STATE_DIR) --
#    NOT /tmp, per the systemd unit's own comment about tmpfiles cleanup
#    risking a stuck episode's stop/evidence file.
# ---------------------------------------------------------------------
echo
echo "-- Creating /var/lib/wardence/state --"
mkdir -p /var/lib/wardence/state
chown "${REAL_USER}:${REAL_USER}" /var/lib/wardence/state

# ---------------------------------------------------------------------
# 7. systemd unit -- fill in the placeholders we DO know (user, paths),
#    leave CORS_ORIGINS/PROMETHEUS_URL as-is until the README's later
#    steps produce real values for them.
# ---------------------------------------------------------------------
echo
echo "-- Installing systemd unit (with real user/path values) --"
sed -e "s|<WARDENCE_USER>|${REAL_USER}|g" \
    -e "s|<REPO_PATH>|${REPO_PATH}|g" \
    -e "s|<VENV_PATH>|${VENV_PATH}|g" \
    "${REPO_PATH}/deploy/operator-api.service" > /etc/systemd/system/wardence-operator-api.service
systemctl daemon-reload
echo "   Installed, NOT started yet -- CORS_ORIGINS/PROMETHEUS_URL in"
echo "   /etc/systemd/system/wardence-operator-api.service still need real"
echo "   values (Vercel domain, Prometheus NodePort) before 'systemctl"
echo "   enable --now wardence-operator-api' will actually be useful."

# ---------------------------------------------------------------------
# 8. Reminders for what's still manual (secrets can't be scripted safely)
# ---------------------------------------------------------------------
echo
echo "== Provisioning done. Still manual, per deploy/README.md: =="
echo "  - Copy repo-root .env (LLM provider keys) to ${REPO_PATH}/.env"
echo "  - Copy p3_trust_action/.env (R2 credentials) to ${REPO_PATH}/p3_trust_action/.env"
echo "  - Regenerate p3_trust_action/sa_token.txt against THIS cluster's"
echo "    wardence-agent ServiceAccount (created in README section 1's"
echo "    rbac.yaml apply -- run that first, then:"
echo "    kubectl create token wardence-agent -n sock-shop --duration=720h > p3_trust_action/sa_token.txt"
echo "  - Run create_admin_account.py / mint_token.py fresh on this host"
echo "    (JWT secret + admin TOTP are per-deployment, not copyable)"
echo
echo "Next: follow deploy/README.md starting at section 1."

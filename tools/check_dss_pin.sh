#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! grep -qE '^[[:space:]]*image:[[:space:]]*atc-dss-hardened:' "${ROOT_DIR}/docker-compose.yml"; then
  echo "[check_dss_pin] Expected docker-compose.yml to use the hardened DSS image (atc-dss-hardened)" >&2
  exit 1
fi

dockerfile="${ROOT_DIR}/dss-hardened/Dockerfile"
if [[ ! -f "${dockerfile}" ]]; then
  echo "[check_dss_pin] Missing hardened DSS Dockerfile at ${dockerfile}" >&2
  exit 1
fi

if ! grep -qF 'rm -rf /test-certs' "${dockerfile}"; then
  echo "[check_dss_pin] Hardened DSS Dockerfile must delete /test-certs" >&2
  exit 1
fi

user_line="$(grep -E '^USER[[:space:]]+' "${dockerfile}" | tail -n 1 || true)"
if [[ -z "${user_line}" ]]; then
  echo "[check_dss_pin] Hardened DSS Dockerfile must set USER" >&2
  exit 1
fi
if echo "${user_line}" | grep -qE '^USER[[:space:]]+(0|root)(:0)?$'; then
  echo "[check_dss_pin] Hardened DSS Dockerfile must not run as root (found: ${user_line})" >&2
  exit 1
fi

expected_image_tag="${DSS_IMAGE_TAG:-}"
if [[ -z "${expected_image_tag}" ]]; then
  raw_tags="$(
    grep -E '^[[:space:]]*image:[[:space:]]*(interuss/dss|atc-dss-hardened):' "${ROOT_DIR}/docker-compose.yml" \
      | sed -E 's/.*(interuss\/dss|atc-dss-hardened):([^[:space:]]+).*/\2/' \
      | sort -u
  )"
  tag_count="$(echo "${raw_tags}" | grep -c '^[^[:space:]]' || true)"
  if [[ "${tag_count}" -ne 1 ]]; then
    echo "[check_dss_pin] Expected exactly one DSS image tag in docker-compose.yml, found:" >&2
    echo "${raw_tags}" >&2
    exit 1
  fi

  # Allow docker-compose substitutions like: ${DSS_IMAGE_TAG:-v0.15.0}
  if [[ "${raw_tags}" == '${DSS_IMAGE_TAG:-'*'}' ]]; then
    expected_image_tag="${raw_tags#'${DSS_IMAGE_TAG:-'}"
    expected_image_tag="${expected_image_tag%'}'}"
  else
    expected_image_tag="${raw_tags}"
  fi
fi

expected_version="${expected_image_tag#v}"
expected_submodule_tag="interuss/dss/v${expected_version}"

submodule_tag="$(git -C "${ROOT_DIR}/interuss-dss" describe --tags --exact-match 2>/dev/null || true)"
if [[ -z "${submodule_tag}" ]]; then
  echo "[check_dss_pin] interuss-dss submodule is not at an exact tag; expected ${expected_submodule_tag}" >&2
  exit 1
fi

if [[ "${submodule_tag}" != "${expected_submodule_tag}" ]]; then
  echo "[check_dss_pin] DSS pin drift detected:" >&2
  echo "  docker-compose expects interuss/dss:${expected_image_tag}" >&2
  echo "  interuss-dss submodule is at ${submodule_tag}" >&2
  echo "  expected submodule tag: ${expected_submodule_tag}" >&2
  exit 1
fi

echo "[check_dss_pin] OK: interuss/dss:${expected_image_tag} matches submodule ${submodule_tag}"

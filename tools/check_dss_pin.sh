#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

expected_image_tag="${DSS_IMAGE_TAG:-}"
if [[ -z "${expected_image_tag}" ]]; then
  raw_tags="$(
    grep -E '^[[:space:]]*image:[[:space:]]*interuss/dss:' "${ROOT_DIR}/docker-compose.yml" \
      | sed -E 's/.*interuss\/dss:([^[:space:]]+).*/\1/' \
      | sort -u
  )"
  tag_count="$(echo "${raw_tags}" | grep -c '^[^[:space:]]' || true)"
  if [[ "${tag_count}" -ne 1 ]]; then
    echo "[check_dss_pin] Expected exactly one interuss/dss image tag in docker-compose.yml, found:" >&2
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

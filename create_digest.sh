#!/bin/bash
# Kurzform für specialties/create_digest.sh (legt ein neues Fachgebiet an).
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/specialties/create_digest.sh" "$@"

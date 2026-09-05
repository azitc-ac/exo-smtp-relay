# Basisabbild AUF DEN DIGEST festgenagelt — derselbe Index-Digest wie im grossen
# Gateway (python 3.11.15-slim-trixie), damit beide Dienste auf demselben
# geprüften Unterbau laufen. Arch-übergreifend: amd64 UND arm64 (Raspberry Pi).
#
# AKTUALISIEREN: `docker buildx imagetools inspect python:3.11-slim` liefert den
# neuen Index-Digest. Danach bauen, testen, erst dann ausrollen.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS base

WORKDIR /app

# ── Systempakete ──────────────────────────────────────────────────────────────
# Bewusst nicht gepinnt (Debian entfernt alte Fassungen aus dem Spiegel) — der
# Weg, auf dem Sicherheitsaktualisierungen von OpenSSL ins Abbild kommen.
# KEIN certbot: Der Dienst steht im eigenen Netz; ein selbstsigniertes oder
# importiertes Zertifikat genügt. libicu braucht PowerShell.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libcap2-bin openssl wget ca-certificates libssl-dev libicu-dev \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_bind_service=+eip $(readlink -f /usr/local/bin/python3)

# ── PowerShell 7 (arch-aware, gegen SHA256 geprüft) ──────────────────────────
# Für das ExchangeOnlineManagement-Modul: Postfachliste und Inbound-Connector.
# Beim Anheben von PS_VERSION BEIDE Hashes aus
# https://github.com/PowerShell/PowerShell/releases/download/v<VERSION>/hashes.sha256
# mit übernehmen — sonst schlägt der Build fehl (genau das ist der Zweck).
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    PS_VERSION="7.6.2"; \
    case "${ARCH}" in \
        amd64)   PS_ARCH="x64";   PS_SHA="6cbcfbf20e376aa62ffd91c973493c41a7a52ddfd5a5db3ff9bc12f0d0fe9292" ;; \
        arm64)   PS_ARCH="arm64"; PS_SHA="a8d4e386dfafda385d0604045eed03ce6f3a843d45fc8f0b9588b836ca17cdb8" ;; \
        *)       echo "Unsupported arch: ${ARCH}" && exit 1 ;; \
    esac; \
    PS_URL="https://github.com/PowerShell/PowerShell/releases/download/v${PS_VERSION}/powershell-${PS_VERSION}-linux-${PS_ARCH}.tar.gz"; \
    mkdir -p /opt/microsoft/powershell/7; \
    wget -q -O /tmp/pwsh.tar.gz "${PS_URL}"; \
    echo "${PS_SHA}  /tmp/pwsh.tar.gz" | sha256sum -c -; \
    tar -xz -C /opt/microsoft/powershell/7 -f /tmp/pwsh.tar.gz; \
    rm /tmp/pwsh.tar.gz; \
    chmod +x /opt/microsoft/powershell/7/pwsh; \
    ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh

# ── ExchangeOnlineManagement — FASSUNG FESTGENAGELT (3.10.1 wie im Gateway) ──
# ⚠️ Nur NATIV bauen (amd64 auf amd64, arm64 auf arm64): pwsh unter QEMU-
# Emulation stürzt hier mit SIGABRT ab (siehe Gateway-Dockerfile).
RUN pwsh -NoProfile -NonInteractive -Command \
    "Set-PSRepository PSGallery -InstallationPolicy Trusted; \
     Install-Module ExchangeOnlineManagement -RequiredVersion 3.10.1 \
       -Force -AllowClobber -Scope AllUsers"

# ── Python-Abhängigkeiten — aus der LOCK-Datei ───────────────────────────────
COPY app/requirements.txt app/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# ── Anwendung ────────────────────────────────────────────────────────────────
COPY app/ .
COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md

ENV DATA_DIR=/app/data

# Mountpunkt VOR dem chown anlegen, sonst gehört er root und appuser kann beim
# ersten Start nicht schreiben (Crash-Loop).
RUN mkdir -p /app/data && useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 25 8080
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fk https://localhost:8080/health || curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]

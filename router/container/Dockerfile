# IntentQoS as an OCI container for prplOS Life Cycle Management (LCM) or any container-capable gateway.
# Build for the gateway's CPU, e.g.:  docker buildx build --platform linux/arm64 -t intentqos:1.0.0 -f router/container/Dockerfile .
# Run with the host network and the two capabilities the engine needs (no privileged mode):
#   docker run -d --network host --cap-add NET_ADMIN --cap-add NET_RAW -v /etc/intentqos:/etc/intentqos intentqos:1.0.0
# (Not built in this repository's CI: the build host has no access to a container registry.)
FROM python:3.11-alpine
RUN apk add --no-cache iproute2 iproute2-tc nftables
COPY intentqos/ /usr/lib/intentqos/intentqos/
COPY models/flowclf.iqm /usr/lib/intentqos/models/flowclf.iqm
ARG ARCH=aarch64
COPY router/bin/iq-capture.${ARCH} /usr/sbin/iq-capture
COPY router/container/entrypoint.sh /entrypoint.sh
ENV PYTHONPATH=/usr/lib/intentqos
ENTRYPOINT ["/entrypoint.sh"]

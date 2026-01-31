ARG DSS_IMAGE_TAG=v0.15.0
FROM interuss/dss:${DSS_IMAGE_TAG}

# Remove upstream test certificates from the runtime image. The demo stack can still
# mount sandbox keys/certs explicitly via docker-compose.
RUN rm -rf /test-certs

# Run as non-root in the hardened profile.
RUN addgroup -S -g 10000 dss && adduser -S -D -u 10000 -G dss dss
WORKDIR /tmp
USER 10000:10000

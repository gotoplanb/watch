# AWS Emulator (ADR-067) — AWS publishes no image, so we build one from its signed Linux package.
# VERSION is pinned so a release can't change test results overnight; bump it on purpose
# (versions: https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/versions.json).
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
ARG VERSION
ARG ARCH=aarch64
ADD https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/aws-emulator-${VERSION}-1-${ARCH}.rpm /tmp/aws-emulator.rpm
RUN rpm -i /tmp/aws-emulator.rpm && rm /tmp/aws-emulator.rpm \
    && mkdir -p /home/emulator && chown 1000 /home/emulator
USER 1000
# Listen inside the container; `make emulator-up` publishes the port on 127.0.0.1 only.
ENV HOME=/home/emulator EMULATOR_BIND_ADDRESS=0.0.0.0 EMULATOR_TELEMETRY=off
ENTRYPOINT ["aws-emulator"]

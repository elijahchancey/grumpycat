# Example deployment runtime image. Copy into your own infra repo, pin the version, add the
# toolchains and CLIs your repositories' agent skills use. Keep it to one image per deployment.
FROM public.ecr.aws/REPLACE_ME/grumpycat-worker:v0.1.0

USER root
# Example: a Ruby + Node organisation that uses Sentry and Datadog from in-repo skills.
# RUN apt-get update && apt-get install -y --no-install-recommends ruby-full build-essential \
#       libpq-dev && rm -rf /var/lib/apt/lists/*
# RUN npm install -g @datadog/datadog-ci @sentry/cli
USER grumpycat

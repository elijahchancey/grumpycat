# Lambda image: all handlers, all in-tree plugins. Published as grumpycat-lambda.
FROM public.ecr.aws/lambda/python:3.14

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_PROJECT_ENVIRONMENT=/var/lang

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra all && rm -rf /src/src

WORKDIR ${LAMBDA_TASK_ROOT}
# Handler is selected per function by the Terraform module, e.g. grumpycat.handlers.router.handler
CMD ["grumpycat.handlers.router.handler"]

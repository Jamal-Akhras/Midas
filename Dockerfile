# Midas in a container. Build:  docker build -t midas .
# Run the monitor loop against a mounted data directory:
#   docker run --rm -v midas-data:/data --env-file .env midas monitor --loop
# Run the workstation UI (the port is only reachable if you publish it):
#   docker run --rm -p 127.0.0.1:8420:8420 -v midas-data:/data --env-file .env \
#       midas ui --host 0.0.0.0
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY midas ./midas
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 midas && mkdir /data && chown midas:midas /data
COPY --from=build /install /usr/local
USER midas
WORKDIR /data
# the sqlite ledger lives on the mounted volume, never in the image layer
ENV DATABASE_URL=sqlite:////data/midas.db
EXPOSE 8420
ENTRYPOINT ["midas"]
CMD ["monitor", "--loop"]

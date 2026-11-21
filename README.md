# Blast Radius

Blast Radius selects SQLGlot tests for a unified diff and schedules them across one to eight workers. It uses the bundled coverage map to match changed Python code and SQL fixtures to tests. When it cannot localize a change, it selects the full suite.

## Run the sample with Docker

From the repository root, set `KIT` to the absolute path of the extracted starter kit. The container reads the sample from `/data` and writes one JSON prediction per line.

```sh
KIT=/absolute/path/to/starter-kit
docker build --platform linux/amd64 -t blast-radius .
docker run --rm --platform linux/amd64 --network none --read-only \
  --cpus 2 --memory 4g --tmpfs /tmp:size=64m -e HOME=/tmp \
  -v "$KIT/sample:/data:ro" blast-radius > predictions.jsonl
python "$KIT/score.py" "$KIT/sample" "$KIT/sample/answers.json" predictions.jsonl
```

## Build on Linux

Install a C++20 compiler and CPython development headers. Set `SRC` to a clean SQLGlot checkout at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and use the same starter kit as above.

```sh
SRC=/absolute/path/to/sqlglot
g++ -std=c++20 -O2 $(python3-config --includes) selector.cpp $(python3-config --embed --ldflags) -o selector
./selector "$KIT/sample" "$SRC" coverage.json > predictions.jsonl
```

## Regenerate the coverage map

The bundled map covers 1,545 of 1,620 catalog tests. To rebuild it, install Python 3.12 or newer and the starter kit's test dependencies, including DuckDB.

```sh
python build_map.py "$SRC" "$KIT/sample/tests.json" coverage.json
```

# Third-party licences

The two deployed artifacts that `scripts/build_assets.sh` builds bundle the third-party Python
packages below, transitive dependencies included. The tables were generated from the built
artifacts with [pip-licenses](https://github.com/raimon49/pip-licenses) 5.5.5 on 11 November 2026
(see [Regenerating](#regenerating)); the versions are the ones the pins in `requirements/` resolved
to for Python 3.13 on arm64 Linux. Every package ships its full licence text, and its NOTICE file
where it has one, in its `*.dist-info/` folder inside the artifact. The URL column is the project
URL from each package's own metadata.

## Harness worker Lambda layer (`.build/layers/agent`, from `requirements/worker.txt`)

| Name                                    | Version     | License                              | URL                                                                                                                    |
|-----------------------------------------|-------------|--------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| PyJWT                                   | 2.15.1      | MIT                                  | https://github.com/jpadilla/pyjwt                                                                                      |
| PyYAML                                  | 6.0.3       | MIT License                          | https://pyyaml.org/                                                                                                    |
| annotated-types                         | 0.8.0       | MIT                                  | https://github.com/annotated-types/annotated-types                                                                     |
| anyio                                   | 4.16.0      | MIT                                  | https://anyio.readthedocs.io/en/stable/versionhistory.html                                                             |
| attrs                                   | 26.1.0      | MIT                                  | https://www.attrs.org/en/stable/changelog.html                                                                         |
| boto3                                   | 1.43.123    | Apache-2.0                           | https://github.com/boto/boto3                                                                                          |
| botocore                                | 1.43.127    | Apache-2.0                           | https://github.com/boto/botocore                                                                                       |
| certifi                                 | 2026.10.23  | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi                                                                              |
| cffi                                    | 2.1.1       | MIT-0                                | https://cffi.readthedocs.io/en/latest/whatsnew.html                                                                    |
| click                                   | 8.5.1       | BSD-3-Clause                         | https://github.com/pallets/click/                                                                                      |
| cryptography                            | 50.0.2      | Apache-2.0 OR BSD-3-Clause           | https://github.com/pyca/cryptography                                                                                   |
| docstring_parser                        | 0.18.0      | MIT License                          | https://github.com/rr-/docstring_parser                                                                                |
| h11                                     | 0.16.0      | MIT License                          | https://github.com/python-hyper/h11                                                                                    |
| httpcore                                | 1.0.9       | BSD-3-Clause                         | https://www.encode.io/httpcore/                                                                                        |
| httpcore2                               | 2.16.0      | BSD-3-Clause                         | https://github.com/pydantic/httpx2                                                                                     |
| httpx                                   | 0.28.1      | BSD License                          | https://github.com/encode/httpx                                                                                        |
| httpx2                                  | 2.16.0      | BSD-3-Clause                         | https://github.com/pydantic/httpx2                                                                                     |
| idna                                    | 3.20        | BSD-3-Clause                         | https://github.com/kjd/idna                                                                                            |
| jmespath                                | 1.1.0       | MIT License                          | https://github.com/jmespath/jmespath.py                                                                                |
| jsonschema                              | 4.26.0      | MIT                                  | https://github.com/python-jsonschema/jsonschema                                                                        |
| jsonschema-specifications               | 2025.9.1    | MIT                                  | https://github.com/python-jsonschema/jsonschema-specifications                                                         |
| mcp                                     | 2.3.0       | MIT License                          | https://modelcontextprotocol.io                                                                                        |
| mcp-types                               | 2.3.0       | MIT License                          | https://modelcontextprotocol.io                                                                                        |
| opentelemetry-api                       | 1.45.2      | Apache-2.0                           | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api                                     |
| opentelemetry-instrumentation           | 0.66b2      | Apache-2.0                           | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/opentelemetry-instrumentation                 |
| opentelemetry-instrumentation-threading | 0.66b2      | Apache-2.0                           | https://github.com/open-telemetry/opentelemetry-python-contrib/instrumentation/opentelemetry-instrumentation-threading |
| opentelemetry-sdk                       | 1.45.2      | Apache-2.0                           | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk                                     |
| opentelemetry-semantic-conventions      | 0.66b2      | Apache-2.0                           | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions                    |
| packaging                               | 26.3        | Apache-2.0 OR BSD-2-Clause           | https://github.com/pypa/packaging                                                                                      |
| pycparser                               | 3.12        | BSD-3-Clause                         | https://github.com/eliben/pycparser                                                                                    |
| pydantic                                | 2.14.3      | MIT                                  | https://github.com/pydantic/pydantic                                                                                   |
| pydantic_core                           | 2.50.4      | MIT                                  | https://github.com/pydantic                                                                                            |
| python-dateutil                         | 2.9.0.post0 | Apache Software License; BSD License | https://github.com/dateutil/dateutil                                                                                   |
| python-multipart                        | 0.0.33      | Apache-2.0                           | https://github.com/Kludex/python-multipart                                                                             |
| referencing                             | 0.37.0      | MIT                                  | https://github.com/python-jsonschema/referencing                                                                       |
| rpds-py                                 | 2026.9.1    | MIT                                  | https://github.com/crate-py/rpds                                                                                       |
| s3transfer                              | 0.19.3      | Apache Software License              | https://github.com/boto/s3transfer                                                                                     |
| six                                     | 1.17.0      | MIT License                          | https://github.com/benjaminp/six                                                                                       |
| sse-starlette                           | 3.7.0       | BSD-3-Clause                         | https://github.com/sysid/sse-starlette                                                                                 |
| starlette                               | 1.10.0      | BSD-3-Clause                         | https://github.com/Kludex/starlette                                                                                    |
| strands-agents                          | 1.64.0      | Apache Software License              | https://github.com/strands-agents/harness-sdk                                                                          |
| truststore                              | 0.10.5      | MIT                                  | https://github.com/sethmlarson/truststore                                                                              |
| typing-inspection                       | 0.4.5       | MIT                                  | https://github.com/pydantic/typing-inspection                                                                          |
| typing_extensions                       | 4.16.0      | PSF-2.0                              | https://github.com/python/typing_extensions                                                                            |
| urllib3                                 | 2.8.0       | MIT                                  | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst                                                               |
| uvicorn                                 | 0.54.0      | BSD-3-Clause                         | https://uvicorn.dev/                                                                                                   |
| watchdog                                | 6.0.0       | Apache Software License              | https://github.com/gorakhargosh/watchdog                                                                               |
| wrapt                                   | 2.5.2       | BSD-2-Clause                         | https://github.com/GrahamDumpleton/wrapt                                                                               |

## AgentCore Runtime package (`.build/runtime`, from `requirements/runtime.txt`)

| Name            | Version     | License                              | URL                                                      |
|-----------------|-------------|--------------------------------------|----------------------------------------------------------|
| boto3           | 1.43.123    | Apache-2.0                           | https://github.com/boto/boto3                            |
| botocore        | 1.43.127    | Apache-2.0                           | https://github.com/boto/botocore                         |
| jmespath        | 1.1.0       | MIT License                          | https://github.com/jmespath/jmespath.py                  |
| python-dateutil | 2.9.0.post0 | Apache Software License; BSD License | https://github.com/dateutil/dateutil                     |
| s3transfer      | 0.19.3      | Apache Software License              | https://github.com/boto/s3transfer                       |
| six             | 1.17.0      | MIT License                          | https://github.com/benjaminp/six                         |
| urllib3         | 2.8.0       | MIT                                  | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |

The runtime package ships no `certifi`; botocore uses its own bundled CA store.

## Other deployed code

The Lambda functions run this sample's own code from `src/` with no bundled dependencies (the
worker gets the layer above). The stack's two CDK custom-resource handlers, which delete bucket
objects on teardown in sandbox deployments and remove the VPC default security group rules, are
`aws-cdk-lib` code (Apache-2.0) and use the AWS SDK for JavaScript that the Lambda runtime provides.

## Build and CDK tooling (not deployed)

The CDK CLI (`aws-cdk`, Apache-2.0) is pinned in `package.json` and runs only on the machine you
deploy from. `aws-cdk-lib` and `constructs` (both Apache-2.0) synthesize the stack at build time;
apart from the two handlers above, none of their code is deployed. The test tools in
`requirements/dev.txt` run only at build time. The diagram PNGs under `docs/diagrams/` were
produced with [archify](https://github.com/tt-a1i/archify) (MIT); archify itself is not a
dependency of this sample.

## Regenerating

After changing `requirements/`, rebuild the artifacts and regenerate both tables. Each table is
read through an empty interpreter that sees only that artifact, so nothing from your own
environment is listed:

```bash
./scripts/build_assets.sh
.venv/bin/pip install pip-licenses==5.5.5
for artifact in layers/agent/python runtime; do
  env=".build/licences-${artifact%%/*}"
  python3 -m venv --without-pip "$env"
  echo "$PWD/.build/$artifact" > "$(echo "$env"/lib/python3*/site-packages)/artifact.pth"
  .venv/bin/pip-licenses --python "$env/bin/python" --with-system --format=markdown --with-urls --order=name
done
```

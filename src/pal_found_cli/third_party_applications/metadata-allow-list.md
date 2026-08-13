# Third-Party Applications metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `third_party_applications.third_party_application.get` | PERMITTED | Third-party application metadata |
| `third_party_applications.version.delete` | BLOCKED | Deletes a Website version |
| `third_party_applications.version.get` | PERMITTED | Website version metadata |
| `third_party_applications.version.list` | PERMITTED | Website version list metadata |
| `third_party_applications.version.upload` | BLOCKED | Uploads a Website version zip |
| `third_party_applications.version.upload_snapshot` | BLOCKED | Uploads a snapshot version zip |
| `third_party_applications.website.deploy` | BLOCKED | Deploys a Website version |
| `third_party_applications.website.get` | PERMITTED | Website metadata |
| `third_party_applications.website.undeploy` | BLOCKED | Undeploys the Website |

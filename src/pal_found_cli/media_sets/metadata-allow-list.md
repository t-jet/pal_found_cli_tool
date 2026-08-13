# Media Sets metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `media_sets.media_set.abort` | BLOCKED | Aborts a transaction |
| `media_sets.media_set.calculate` | BLOCKED | Starts a transformation job |
| `media_sets.media_set.clear` | BLOCKED | Clears a media item path |
| `media_sets.media_set.commit` | BLOCKED | Commits a transaction |
| `media_sets.media_set.create` | BLOCKED | Opens a transaction |
| `media_sets.media_set.get` | PERMITTED | Media set metadata |
| `media_sets.media_set.get_result` | BLOCKED | Transformation result content bytes |
| `media_sets.media_set.get_rid_by_path` | PERMITTED | Media item path metadata |
| `media_sets.media_set.get_status` | PERMITTED | Transformation job status metadata |
| `media_sets.media_set.info` | PERMITTED | Media item metadata |
| `media_sets.media_set.metadata` | PERMITTED | Media item metadata |
| `media_sets.media_set.read` | BLOCKED | Media content bytes |
| `media_sets.media_set.read_original` | BLOCKED | Original media content bytes |
| `media_sets.media_set.reference` | BLOCKED | Media reference content |
| `media_sets.media_set.register` | BLOCKED | Registers a media item |
| `media_sets.media_set.retrieve` | BLOCKED | Thumbnail content bytes |
| `media_sets.media_set.transform` | BLOCKED | Runs a transformation |
| `media_sets.media_set.upload` | BLOCKED | Uploads media content |
| `media_sets.media_set.upload_media` | BLOCKED | Uploads temporary media content |

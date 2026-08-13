# Streams metadata allow-list

| SDK Path | Status | Rationale |
| --- | --- | --- |
| `streams.dataset.create` | BLOCKED | Creates a streaming dataset |
| `streams.stream.create` | BLOCKED | Creates a stream |
| `streams.stream.get` | PERMITTED | Stream metadata |
| `streams.stream.get_end_offsets` | PERMITTED | Offset metadata |
| `streams.stream.get_records` | BLOCKED | Streams record content bytes |
| `streams.stream.publish_binary_record` | BLOCKED | Publishes record content |
| `streams.stream.publish_record` | BLOCKED | Publishes record content |
| `streams.stream.publish_records` | BLOCKED | Publishes record content |
| `streams.stream.reset` | BLOCKED | Resets stream state |
| `streams.subscriber.commit_offsets` | BLOCKED | Mutates subscriber offsets |
| `streams.subscriber.create` | BLOCKED | Creates a subscriber |
| `streams.subscriber.delete` | BLOCKED | Deletes a subscriber |
| `streams.subscriber.get_read_position` | PERMITTED | Subscriber offset metadata |
| `streams.subscriber.read_records` | BLOCKED | Streams record content bytes |
| `streams.subscriber.reset_offsets` | BLOCKED | Mutates subscriber offsets |

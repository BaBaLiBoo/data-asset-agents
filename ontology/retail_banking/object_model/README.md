# MiniBank object model seed

This directory is the direct, reviewed object-model seed used by the default demo. It defines
business objects and properties separately from physical bindings, business links separately from
physical joins, and analytical Metric/Dimension definitions exclusively through Property IDs. It
does not depend on the parent directory's compatibility `concepts.yaml`, `metrics.yaml`,
`dimensions.yaml`, or `mappings.yaml`, nor on the legacy migrator.

The seed creates a Draft only. Schema inspection, validation, dynamic Dry Run, human review, and
publication remain mandatory. All names and data contracts describe the fictional MiniBank domain.

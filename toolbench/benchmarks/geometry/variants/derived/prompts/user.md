Two 2-D points `p1` and `p2` are defined by the following constructions:

- `p1` is the **centroid** (component-wise mean) of the three points
  `(-1, -2)`, `(0, 0)`, and `(1, 2)`.
- `p2` is the **intersection point** of the two lines `y = 2x - 2` and
  `y = -x + 7`.

Derive `p1` and `p2` from these descriptions, then compute:

1. the Euclidean distance between `p1` and `p2`, and
2. their midpoint (component-wise average).

Write the result to `output/answer.json` as a single JSON object:

```json
{"distance": <number>, "midpoint": [<number>, <number>]}
```

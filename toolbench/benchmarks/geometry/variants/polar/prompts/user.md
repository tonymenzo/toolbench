Two 2-D points `p1` and `p2` are specified in **polar coordinates**
`(ρ, θ)`, with `θ` in **radians**, by the following constructions:

- For `p1`: `ρ₁` is the smallest non-negative solution of `ρ² − 6ρ = 0`;
  `θ₁ = π/6`.
- For `p2`: `ρ₂` is the hypotenuse of a right triangle with legs `3` and
  `4`; `θ₂ = arctan(4/3)`.

Convert each polar point to its Cartesian `(x, y)` representation
**before** computing anything else. Then, working in Cartesian
coordinates, compute:

1. the Euclidean distance between the two **Cartesian** points, and
2. their midpoint (component-wise average of the **Cartesian**
   coordinates).

Write the result to `output/answer.json` as a single JSON object:

```json
{"distance": <number>, "midpoint": [<number>, <number>]}
```

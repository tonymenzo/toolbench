# Distance + midpoint

Given two points `p1 = [x1, y1]` and `p2 = [x2, y2]`:

1. `dx = subtract(x2, x1)`, `dy = subtract(y2, y1)`
2. `distance = power( add(power(dx, 2), power(dy, 2)), 0.5 )`
3. `midpoint = [ divide(add(x1, x2), 2), divide(add(y1, y2), 2) ]`

Write the result to `output/answer.json` as `{"distance": ..., "midpoint": [...]}`.

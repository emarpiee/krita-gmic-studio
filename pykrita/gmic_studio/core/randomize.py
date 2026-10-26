"""Random parameter values for a filter: from 'anything in range' down to a small nudge."""
import random as _random

LOCKABLE = ("int", "float", "bool", "choice", "color", "point")


def _nudge(current, lo, hi, strength, rng):
    span = (hi - lo) * strength
    return max(lo, min(hi, current + rng.uniform(-span, span)))


def random_value(param, current, rng, strength=1.0):
    """A random value for one parameter. strength 1 = anywhere in its range; smaller = stay near `current`."""
    k = param.kind
    full = strength >= 1.0
    if k == "int":
        lo, hi = int(param.minimum), int(param.maximum)
        return rng.randint(lo, hi) if full else int(round(_nudge(current, lo, hi, strength, rng)))
    if k == "float":
        lo, hi = float(param.minimum), float(param.maximum)
        return rng.uniform(lo, hi) if full else _nudge(float(current), lo, hi, strength, rng)
    if k == "bool":
        return rng.random() < 0.5 if full else (not current if rng.random() < strength * 0.5 else bool(current))
    if k == "choice":
        n = max(1, len(param.choices))
        return rng.randrange(n) if full or rng.random() < strength * 0.5 else current
    if k == "color":
        cur = list(current)
        rgb = [rng.randint(0, 255) for _ in range(3)] if full else [int(round(_nudge(c, 0, 255, strength, rng))) for c in cur[:3]]
        return rgb + cur[3:]                                             # alpha, if the filter has one, is left alone
    if k == "point":
        if full:
            return [rng.uniform(0.0, 100.0), rng.uniform(0.0, 100.0)]
        return [_nudge(current[0], 0.0, 100.0, strength, rng), _nudge(current[1], 0.0, 100.0, strength, rng)]
    return current                                                       # text, files, buttons, hidden values: never touched


def randomize(fdef, values, locked=(), rng=None, strength=1.0):
    """New values for `fdef` (a dict keyed by parameter index as text). Locked indices keep their value."""
    rng = rng or _random
    out = dict(values)
    locked = {str(i) for i in locked}
    for i, p in enumerate(fdef.params):
        key = str(i)
        if key in locked or p.kind not in LOCKABLE or p.visibility == 0:
            continue
        out[key] = p.coerce(random_value(p, values.get(key, p.default), rng, strength))
    return out

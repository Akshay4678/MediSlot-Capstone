"""An in-memory DynamoDB table for tests, faithful where it matters to the booking engine.

* Each call is atomic (one lock per table), like single-item operations in DynamoDB.
* ConditionExpression and UpdateExpression are really parsed and evaluated (a small interpreter for
  the subset MediSlot uses), so a wrong expression fails here, not only in AWS.
* Like DynamoDB it REJECTS: attribute names written without a #alias, placeholders that are not
  supplied, and supplied placeholders that the expression does not use.
* A failed condition raises an error whose response code is ConditionalCheckFailedException.
* hooks: `on_get` is called after every get_item returns (tests use it to line threads up so that
  many bookings really read the same version before any of them writes).
"""

import re
import threading

MISSING = object()


class FakeClientError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class ExpressionError(FakeClientError):
    def __init__(self, message):
        super().__init__("ValidationException", message)


_TOKEN = re.compile(r"\s*(<=|>=|<>|=|<|>|\(|\)|,|\+|-|#[A-Za-z_][\w]*|:[A-Za-z_][\w]*|[A-Za-z_][\w]*)")


def _tokens(text):
    pos, out = 0, []
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m:
            raise ExpressionError(f"cannot parse expression near {text[pos:pos + 12]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Parser:
    FUNCTIONS = {"attribute_not_exists", "attribute_exists", "if_not_exists"}

    def __init__(self, text, names, values, item):
        self.t, self.i = _tokens(text), 0
        self.names, self.values, self.item = names or {}, values or {}, item
        self.used_names, self.used_values = set(), set()

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, expected=None):
        tok = self.peek()
        if tok is None or (expected and tok.upper() != expected.upper()):
            raise ExpressionError(f"expected {expected!r} but found {tok!r}")
        self.i += 1
        return tok

    def attr(self, tok):
        if not tok.startswith("#"):
            raise ExpressionError(f"attribute name {tok!r} must use an ExpressionAttributeNames alias (#name)")
        if tok not in self.names:
            raise ExpressionError(f"ExpressionAttributeNames has no value for {tok}")
        self.used_names.add(tok)
        return self.names[tok]

    def operand(self):
        tok = self.take()
        if tok.startswith(":"):
            if tok not in self.values:
                raise ExpressionError(f"ExpressionAttributeValues has no value for {tok}")
            self.used_values.add(tok)
            return self.values[tok]
        if tok.lower() == "if_not_exists":
            self.take("(")
            name = self.attr(self.take())
            self.take(",")
            default = self.operand()
            self.take(")")
            current = self.item.get(name, MISSING) if self.item is not None else MISSING
            return default if current is MISSING else current
        name = self.attr(tok)
        return self.item.get(name, MISSING) if self.item is not None else MISSING

    # ---- conditions
    def condition(self):
        result = self.term()
        while (self.peek() or "").upper() == "OR":
            self.take()
            right = self.term()
            result = result or right
        return result

    def term(self):
        result = self.factor()
        while (self.peek() or "").upper() == "AND":
            self.take()
            right = self.factor()
            result = result and right
        return result

    def factor(self):
        tok = self.peek()
        if tok == "(":
            self.take()
            value = self.condition()
            self.take(")")
            return value
        if tok in ("attribute_not_exists", "attribute_exists"):
            self.take()
            self.take("(")
            name = self.attr(self.take())
            self.take(")")
            exists = self.item is not None and name in self.item
            return (not exists) if tok == "attribute_not_exists" else exists
        left = self.operand()
        op = self.take()
        if op not in ("=", "<>", "<", "<=", ">", ">="):
            raise ExpressionError(f"unsupported operator {op!r}")
        right = self.operand()
        if left is MISSING or right is MISSING:
            return op == "<>" and (left is not right)
        return {"=": left == right, "<>": left != right, "<": left < right, "<=": left <= right,
                ">": left > right, ">=": left >= right}[op]

    # ---- update
    def update(self):
        self.take("SET")
        changes = {}
        while True:
            name = self.attr(self.take())
            self.take("=")
            value = self.operand()
            while self.peek() in ("+", "-"):
                op = self.take()
                other = self.operand()
                if value is MISSING or other is MISSING:
                    raise ExpressionError("The provided expression refers to an attribute that does not exist")
                value = value + other if op == "+" else value - other
            if value is MISSING:
                raise ExpressionError("The provided expression refers to an attribute that does not exist")
            changes[name] = value
            if self.peek() == ",":
                self.take()
                continue
            break
        if self.peek() is not None:
            raise ExpressionError(f"unexpected {self.peek()!r}")
        return changes

    def finish(self, names, values):
        if set(names or {}) != self.used_names:
            raise ExpressionError("ExpressionAttributeNames contains unused or missing names")
        if set(values or {}) != self.used_values:
            raise ExpressionError("ExpressionAttributeValues contains unused or missing values")


class FakeTable:
    def __init__(self, key, on_get=None):
        self.key, self.items, self.lock, self.on_get = key, {}, threading.Lock(), on_get
        self.calls = []

    def _check(self, expression, names, values, item):
        if not expression:
            return
        parser = _Parser(expression, names, values, item)
        ok = parser.condition()
        if parser.peek() is not None:
            raise ExpressionError(f"unexpected {parser.peek()!r}")
        return ok, parser

    def get_item(self, Key, ConsistentRead=False):
        with self.lock:
            item = self.items.get(Key[self.key])
            copy = dict(item) if item else None
        if self.on_get:
            self.on_get()
        return {"Item": copy} if copy else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        with self.lock:
            self.calls.append("put")
            current = self.items.get(Item[self.key])
            if ConditionExpression:
                ok, parser = self._check(ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, current)
                parser.finish(ExpressionAttributeNames, ExpressionAttributeValues)
                if not ok:
                    raise FakeClientError("ConditionalCheckFailedException", "The conditional request failed")
            elif ExpressionAttributeNames or ExpressionAttributeValues:
                raise ExpressionError("placeholders supplied without an expression")
            self.items[Item[self.key]] = dict(Item)
        return {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None):
        with self.lock:
            self.calls.append("update")
            current = self.items.get(Key[self.key])
            used_n, used_v = set(), set()
            if ConditionExpression:
                ok, parser = self._check(ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, current)
                used_n |= parser.used_names
                used_v |= parser.used_values
                if not ok:
                    # DynamoDB validates the whole request before evaluating the condition.
                    if current is not None:
                        _Parser(UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, current).update()
                    raise FakeClientError("ConditionalCheckFailedException", "The conditional request failed")
            base = dict(current) if current is not None else dict(Key)
            up = _Parser(UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, base)
            changes = up.update()
            used_n |= up.used_names
            used_v |= up.used_values
            if set(ExpressionAttributeNames or {}) != used_n or set(ExpressionAttributeValues or {}) != used_v:
                raise ExpressionError("ExpressionAttributeNames/Values contain unused entries")
            base.update(changes)
            self.items[Key[self.key]] = base
        return {}

    def delete_item(self, Key, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        with self.lock:
            current = self.items.get(Key[self.key])
            if ConditionExpression:
                ok, parser = self._check(ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, current)
                parser.finish(ExpressionAttributeNames, ExpressionAttributeValues)
                if not ok:
                    raise FakeClientError("ConditionalCheckFailedException", "The conditional request failed")
            self.items.pop(Key[self.key], None)
        return {}

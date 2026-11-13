"""Before-call limits and conservative, preregistered-price cost reservations."""
from dataclasses import asdict
import math


class BudgetExceeded(RuntimeError):
    pass


def known_cost(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class CostAccount:
    def __init__(self, cap, spent=0.0):
        if not known_cost(cap) or cap <= 0 or not known_cost(spent):
            raise BudgetExceeded('invalid or unknown experiment cost budget')
        self.cap, self.spent, self.reserved = float(cap), float(spent), 0.0
        self.unknown = False

    def reserve(self, amount):
        if not known_cost(amount) or self.unknown or self.spent + self.reserved + amount > self.cap:
            raise BudgetExceeded('cost unknown or insufficient remaining experiment budget')
        self.reserved += amount

    def settle(self, reservation, actual):
        self.reserved = max(0.0, self.reserved - reservation)
        if not known_cost(actual):
            self.unknown = True
        else:
            self.spent += actual


class BudgetClient:
    def __init__(self, client, protocol, case, *, synthetic=False, account=None):
        self.client, self.synthetic = client, synthetic
        self.max_turns = min(protocol.budget['max_turns'], case.budget['max_turns'])
        self.max_tools = min(protocol.budget['max_tool_calls'],
                             case.budget.get('max_tool_calls') if case.budget.get('max_tool_calls') is not None
                             else protocol.budget['max_tool_calls'])
        self.pricing = protocol.pricing
        self.account = account or CostAccount(protocol.budget['total_cost_usd'])
        self.trial_cap = min(protocol.budget['total_cost_usd'],
                             case.budget.get('max_cost_usd') if case.budget.get('max_cost_usd') is not None
                             else protocol.budget['total_cost_usd'])
        self.model_calls = self.tool_calls = 0
        self.spent = 0.0
        self.unknown_cost = False
        self.violation = None
        self.token_totals = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0,
                             'reasoning': 0, 'total_tokens': 0}
        self.usage_calls = 0

    def _stop(self, message):
        self.violation = message
        raise BudgetExceeded(message)

    def _reserve(self, request):
        if self.synthetic:
            return 0.0
        if not self.pricing or self.unknown_cost:
            self._stop('real model calls require preregistered pricing and known prior costs')
        input_rate = self.pricing.get('input_usd_per_mtok')
        output_rate = self.pricing.get('output_usd_per_mtok')
        if not known_cost(input_rate) or not known_cost(output_rate):
            self._stop('invalid preregistered pricing')
        output = request.max_tokens or request.model.max_tokens
        context = request.model.context_window
        if type(output) is not int or output <= 0 or type(context) is not int or context <= 0:
            self._stop('a real call needs bounded context and output token limits')
        maximum = (context * input_rate + output * output_rate) / 1_000_000
        if self.spent + maximum > self.trial_cap:
            self._stop('insufficient remaining trial budget for maximum request cost')
        self.account.reserve(maximum)
        return maximum

    async def stream(self, request):
        from chagent.domain.messages import ToolCall
        from chagent.models.events import DoneEvent, ErrorEvent

        if self.model_calls >= self.max_turns:
            self._stop('model call budget exhausted')
        try:
            reservation = self._reserve(request)
        except BudgetExceeded as error:
            self.violation = str(error)
            raise
        self.model_calls += 1
        terminal = False
        try:
            async for event in self.client.stream(request):
                if isinstance(event, (DoneEvent, ErrorEvent)):
                    message = event.message if isinstance(event, DoneEvent) else event.error
                    usage = asdict(message.usage) if message.usage is not None else None
                    if usage is not None:
                        self.usage_calls += 1
                        for key in self.token_totals:
                            if type(usage.get(key)) is int:
                                self.token_totals[key] += usage[key]
                    actual = (0.0 if self.synthetic else
                              ((sum(usage.get(key, 0) for key in ('input', 'cache_read', 'cache_write'))
                                * self.pricing['input_usd_per_mtok']
                                + usage['output'] * self.pricing['output_usd_per_mtok']) / 1_000_000
                               if usage is not None else None))
                    self.account.settle(reservation, actual)
                    terminal = True
                    if not known_cost(actual):
                        self.unknown_cost = True
                    else:
                        self.spent += actual
                        if actual > reservation + 1e-9 and not self.synthetic:
                            self.violation = 'reported charge exceeded the conservative reservation'
                        if self.account.spent > self.account.cap or self.spent > self.trial_cap:
                            self.violation = 'reported cost exceeded a preregistered cap'
                    tools = sum(isinstance(block, ToolCall) for block in message.content)
                    if self.tool_calls + tools > self.max_tools:
                        self._stop('tool call budget exhausted; batch was not executed')
                    self.tool_calls += tools
                yield event
        finally:
            if not terminal:
                self.account.settle(reservation, 0.0 if self.synthetic else None)
                self.unknown_cost = not self.synthetic

    def cost(self):
        return {'usd': None if self.unknown_cost else round(self.spent, 9),
                'source': 'synthetic client; no paid call' if self.synthetic
                          else 'estimated upper bound from preregistered rates, including cached input'}

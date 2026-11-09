"""
Tests for the model catalog: Score fields, prebuilt.Classify, models.Counting and models.Tree,
on the PC and in C (the build checks each model's C answers exactly like its Python).

    python -m unittest discover -s tests -p "test_*.py"
"""
import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_python_api import CC, load  # noqa: E402
from tiny_langgraph import GraphError, models  # noqa: E402
from tiny_langgraph.build import BuildError, build  # noqa: E402
from tiny_langgraph.datalog import LogError, load_log, samples  # noqa: E402
from tiny_langgraph.models import ModelError  # noqa: E402

ROOM = '''
from tiny_langgraph import *
from tiny_langgraph.drivers import Clock, DigitalIn, LED

motion = DigitalIn(pin=1)
siren = LED(pin=2)
clock = Clock()

class Room(State):
    seen    = Choice("no", "yes")
    tod     = Choice("day", "night")
    unusual = Score("no", "odd", "alarming")
    risk    = Score("low", "high")

goals = Goals(logged=1, alerted=Goal(4, only_if_wanted=True))

def active(s: Room) -> bool:
    return s.seen == "yes"

@tool(cost=1, achieves=goals.logged)
def log(s: Room):
    pass

@tool(cost=2, achieves=goals.alerted)
def alert(s: Room):
    siren.on()

def sense(s: Room):
    s.seen = "yes" if motion.read() else "no"
    s.tod = "night" if clock.hour() >= 22 or clock.hour() < 6 else "day"

@rules
def assess(s: Room):
    want(goals.logged)
    if s.unusual == "alarming" or s.risk == "high":
        want(goals.alerted)

NOTICE = models.Counting(active=active, clock=clock)
RISK = models.Tree(reads=["tod", "unusual"], max_depth=3)

graph = StateGraph(Room)
graph.add_node("sense", sense)
graph.add_node("notice", prebuilt.Classify({"unusual": "Is the activity unusual for this room?"}, model=NOTICE))
graph.add_node("risk", prebuilt.Classify({"risk": "Is this worth an alert?"}, model=RISK))
graph.add_node("assess", assess)
graph.add_node("plan", prebuilt.plan)
graph.add_node("verify", prebuilt.verify)
graph.add_node("ask", prebuilt.ask)
graph.add_node("act", prebuilt.Act([log, alert]))
graph.add_edge(START, "sense")
graph.add_edge("sense", "notice")
graph.add_edge("notice", "risk")
graph.add_edge("risk", "assess")
graph.add_edge("assess", "plan")
graph.add_edge("plan", "verify")
graph.add_conditional_edges("verify", prebuilt.after_verify, ["act", "ask", "plan"])
graph.add_conditional_edges("ask", prebuilt.after_ask, ["verify", "plan"])
graph.add_edge("act", END)
app = graph.compile(name="room")
'''

RISK_ROWS = [({"tod": "night", "unusual": "alarming"}, "high"), ({"tod": "night", "unusual": "odd"}, "high"),
             ({"tod": "night", "unusual": "no"}, "low"), ({"tod": "day", "unusual": "alarming"}, "high"),
             ({"tod": "day", "unusual": "odd"}, "low"), ({"tod": "day", "unusual": "no"}, "low")]


def normal_days(days=21, step=5, seed=0):
    """A room that's busy in the day (7-22) and quiet at night: one sample per run."""
    rng, out = random.Random(seed), []
    for minute in range(0, days * 1440, step):
        hour = (minute // 60) % 24
        out.append((minute, hour, rng.random() < (0.5 if 7 <= hour < 22 else 0.02)))
    return out


def fitted_room():
    m = load(ROOM)
    m.NOTICE.fit(normal_days())
    m.RISK.fit(RISK_ROWS)
    return m


def run_at(m, s, minute, seen):
    m.clock.set(hour=(minute // 60) % 24, minutes=minute)
    m.motion.value = seen
    return m.app.invoke(s)


class Graph(unittest.TestCase):
    def test_score_must_be_answered(self):
        src = ROOM.replace('graph.add_node("risk", prebuilt.Classify({"risk": "Is this worth an alert?"}, model=RISK))\n', "")
        src = src.replace('graph.add_edge("notice", "risk")\ngraph.add_edge("risk", "assess")', 'graph.add_edge("notice", "assess")')
        with self.assertRaisesRegex(GraphError, "risk is a Score: add a node that answers it"):
            load(src)

    def test_counting_needs_a_score(self):
        src = ROOM.replace('{"unusual": "Is the activity unusual for this room?"}', '{"seen": "?"}')
        with self.assertRaisesRegex(GraphError, "Counting answers a Score"):
            load(src)

    def test_tree_reads_only_choices(self):
        with self.assertRaisesRegex(GraphError, "can't read the field it answers"):
            load(ROOM.replace('reads=["tod", "unusual"]', 'reads=["tod", "risk"]'))

    def test_classify_takes_a_catalog_model(self):
        with self.assertRaisesRegex(Exception, "takes a model from the catalog"):
            load(ROOM.replace("model=RISK", "model=assess"))


class Counting(unittest.TestCase):
    def test_normal_day_stays_calm_and_long_silence_alarms(self):
        m = fitted_room()
        s = m.Room()
        levels = []
        start = 30 * 1440
        rng = random.Random(1)
        for minute in range(start, start + 1440, 5):              # one more normal day
            hour = (minute // 60) % 24
            run_at(m, s, minute, rng.random() < (0.5 if 7 <= hour < 22 else 0.02))
            levels.append(s.unusual)
        self.assertLessEqual(levels.count("alarming"), 12)        # an hour of alarm at most
        for minute in range(start + 1440 + 9 * 60, start + 1440 + 17 * 60, 5):   # 9:00-17:00, nothing moves
            run_at(m, s, minute, False)
        self.assertEqual(s.unusual, "alarming")
        self.assertIn("alert", s.plan)

    def test_needs_data_and_fit(self):
        m = load(ROOM)
        with self.assertRaisesRegex(ModelError, "isn't fitted"):
            run_at(m, m.Room(), 0, True)
        with self.assertRaisesRegex(ModelError, "at least 3 days"):
            m.NOTICE.fit(normal_days(days=1))

    def test_save_and_load(self):
        m = fitted_room()
        path = os.path.join(tempfile.mkdtemp(), "notice.json")
        m.NOTICE.save(path)
        again = models.Counting(active=m.active, clock=m.clock).load(path)
        self.assertEqual(again.table, m.NOTICE.table)

    def test_odd_is_your_setting(self):
        m = load(ROOM)
        data = normal_days()
        loose = models.Counting(active=m.active, clock=m.clock, odd=0.08).fit(data)
        strict = models.Counting(active=m.active, clock=m.clock, odd=0.01).fit(data)
        self.assertEqual((loose.table["p_odd"], strict.table["p_odd"]), (0.08, 0.01))
        counts = []
        for model in (loose, strict):
            model.bind(m.Room, "unusual")
            mem, rng, n = model.new_memory(), random.Random(2), 0
            for minute in range(0, 3 * 1440, 5):
                hour = (minute // 60) % 24
                n += model.level(mem, rng.random() < (0.5 if 7 <= hour < 22 else 0.02), minute, hour) >= 1
            counts.append(n)
        self.assertGreater(counts[0], counts[1])                  # a looser "odd" flags more moments
        with self.assertRaisesRegex(ModelError, "between 0 and 0.1"):
            models.Counting(active=m.active, clock=m.clock, odd=0.2)

    def test_condition_must_be_a_function(self):
        m = load(ROOM)
        with self.assertRaisesRegex(ModelError, "takes a condition"):
            models.Counting(active=lambda s: True, clock=m.clock)


class Tree(unittest.TestCase):
    def test_learns_the_examples(self):
        m = fitted_room()
        for values, label in RISK_ROWS:
            s = m.Room(**values)
            self.assertEqual(m.RISK.predict(s, None), label, values)

    def test_labels_must_be_values(self):
        m = load(ROOM)
        with self.assertRaisesRegex(ModelError, "aren't values of risk"):
            m.RISK.fit([({"tod": "day", "unusual": "no"}, "maybe")])


class DataLog(unittest.TestCase):
    LOG = ("time,epoch,hour,seen,tod,unusual,risk,plan\n"
           "2026-11-09T07:00:00,1762671600,7,yes,day,no,low,log\n"
           "2026-11-09T07:00:30,1762671630,7,no,day,no,low,log\n"
           "time,epoch,hour,seen,tod,unusual,risk,plan\n"           # the next day's file: names again
           "2026-11-10T23:10:00,1762816200,23,no,night,odd,high,log+alert\n"
           "2026-11-10T23:10:30,17628162")                           # cut off by a power cut

    def write(self, text):
        path = os.path.join(tempfile.mkdtemp(), "room-log.csv")
        with open(path, "w", newline="") as f:
            f.write(text)
        return path

    def test_load_and_samples(self):
        m = load(ROOM)
        rows = load_log(self.write(self.LOG))
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[0]["minute"], rows[0]["hour"], rows[2]["plan"]), (1762671600 // 60, 7, "log+alert"))
        self.assertEqual(samples(rows, m.Room, active=m.active),
                         [(1762671600 // 60, 7, True), (1762671630 // 60, 7, False), (1762816200 // 60, 23, False)])

    def test_another_version_of_the_agent(self):
        m = load(ROOM)
        with self.assertRaisesRegex(LogError, "the columns changed"):
            load_log(self.write(self.LOG.replace("tod,unusual", "unusual,tod", 1)))
        rows = load_log(self.write(self.LOG.replace(",yes,day,", ",maybe,day,")))
        with self.assertRaisesRegex(LogError, "seen can't be 'maybe'"):
            samples(rows, m.Room, active=m.active)

    def test_a_gap_is_a_restart_not_a_quiet_spell(self):
        """The board was off 10:00-16:00 on three days: that mustn't teach Counting that
        six quiet hours are normal at 16:00."""
        m = load(ROOM)
        off = {(d, h) for d in (2, 5, 8) for h in range(10, 16)}
        data = [s for s in normal_days() if (s[0] // 1440, s[1]) not in off]
        t = models.Counting(active=m.active, clock=m.clock).fit(data).table
        self.assertLess(t["excess"][16], 100)
        self.assertLess(t["top"][16], 60)


@unittest.skipUnless(CC, "no C compiler")
class Build(unittest.TestCase):
    def test_models_are_checked_in_c(self):
        m = fitted_room()
        logs = []
        build(m.app, "c", log=logs.append)
        self.assertTrue(any("check: notice (models.Counting(active=active)) answers exactly" in x for x in logs), logs)
        self.assertTrue(any("check: risk (models.Tree(reads=['tod', 'unusual'])) answers exactly like your Python in 6 cases"
                            in x for x in logs), logs)
        self.assertTrue(any("exactly like your Python in all 24 situations" in x for x in logs), logs)   # rules read the Scores
        agent_c = open(os.path.join(m.app.dir, "generated", "room_agent.c")).read()
        self.assertIn("tlg_count_step(&s->m_notice", agent_c)
        self.assertIn("room_tree_risk(&s->in)", agent_c)

    def test_unfitted_model_stops_the_build(self):
        m = load(ROOM)
        m.RISK.fit(RISK_ROWS)
        with self.assertRaisesRegex(BuildError, "notice: models.Counting.* isn't fitted"):
            build(m.app, "c", log=lambda _: None)


if __name__ == "__main__":
    unittest.main()

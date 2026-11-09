"""
The feeding station on your PC: the watcher and the feeder agents on a simulated network,
as if their boards were in radio range.

    python run.py
"""
import feeder
import watcher

from tiny_langgraph import Network


def at(hour, minute, day=0):
    """Both boards' clocks: hour of day and minutes since they started."""
    for clock in (watcher.clock, feeder.clock):
        clock.set(hour=hour, minutes=day * 1440 + hour * 60 + minute)


def show(title):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def main():
    net = Network()
    net.board("watcher", watcher.app, watcher.Watcher())
    net.board("feeder", feeder.app, feeder.Feeder())

    show("07:00 breakfast: the watcher asks, the feeder decides and feeds")
    at(7, 0)
    net.run(verbose=True)
    show("07:01 the watcher reads the answer")
    at(7, 1)
    net.run(verbose=True)

    show("18:30 someone feeds the cat by hand (the feeder's button)")
    at(18, 30)
    feeder.button.value = True
    net.run()
    feeder.button.value = False

    show("19:00 dinner: the watcher asks, the feeder's own rule refuses")
    at(19, 0)
    net.run(verbose=True)
    show("19:01 the watcher reads 'refused' and tells the owner")
    at(19, 1)
    net.run(verbose=True)

    show("next day 07:00: the feeder is unplugged")
    net.unplug("feeder")
    at(7, 0, day=1)
    net.run(verbose=True)


if __name__ == "__main__":
    main()

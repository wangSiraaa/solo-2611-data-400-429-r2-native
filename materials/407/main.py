import json
import os
import sys

from pipeline import plan_sync, run


def _parse_args(argv):
    schedule = None
    sync_mode = None
    plan_only = False
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("--sync", "--sync-mode"):
            index += 1
            if index >= len(argv):
                raise SystemExit("--sync requires a mode")
            sync_mode = argv[index]
        elif argument.startswith("--sync="):
            sync_mode = argument.split("=", 1)[1]
        elif argument in ("--plan", "--plan-only"):
            plan_only = True
        elif argument in ("-h", "--help"):
            raise SystemExit(
                "usage: main.py [SCHEDULE] [--sync cycle|per-command] [--plan]\n"
                "  SCHEDULE defaults to $SCHEDULE_JSON\n"
                "  --sync cycle       one Sync per input cycle (default)\n"
                "  --sync per-command one Sync per command\n"
                "  --plan             print the final Sync grouping and exit"
            )
        elif argument.startswith("-"):
            raise SystemExit("unknown option: %s" % argument)
        else:
            if schedule is not None:
                raise SystemExit("only one schedule may be given")
            schedule = argument
        index += 1

    if schedule is None:
        schedule = os.environ.get("SCHEDULE_JSON")
    if schedule is None:
        raise SystemExit("no schedule given (argument or $SCHEDULE_JSON)")
    return schedule, sync_mode, plan_only


if __name__ == "__main__":
    schedule, sync_mode, plan_only = _parse_args(sys.argv[1:])
    if plan_only:
        print(json.dumps(plan_sync(schedule, sync_mode), ensure_ascii=False))
    else:
        print(json.dumps(run(schedule, sync_mode), ensure_ascii=False))

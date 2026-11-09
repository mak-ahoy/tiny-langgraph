#include "scenarios.h"

#define IN(cam, audio, heat, tod, owner, wifi, expect, recent, last) \
    { DOOR_GUARD_CAM_##cam, DOOR_GUARD_AUDIO_##audio, DOOR_GUARD_HEAT_##heat, DOOR_GUARD_TOD_##tod, \
      DOOR_GUARD_OWNER_##owner, DOOR_GUARD_WIFI_##wifi, DOOR_GUARD_EXPECT_##expect, \
      DOOR_GUARD_RECENT_##recent, DOOR_GUARD_LAST_##last }

const dg_scenario_t dg_scenarios[] = {
    { "quiet night, nobody there",
      IN(NONE, NONE, NONE, NIGHT, AWAY, UP, NONE, NONE, NONE), "" },
    { "milkman knocks at his usual time",
      IN(PERSON_HI, KNOCK, PRESENT, MORNING, HOME, UP, MILKMAN, NONE, NONE), "" },
    { "unexpected visitor, afternoon",
      IN(PERSON_MID, VOICE, PRESENT, AFTERNOON, HOME, UP, NONE, NONE, NONE), "" },
    { "stranger at night, owner away",
      IN(PERSON_HI, NONE, PRESENT, NIGHT, AWAY, UP, NONE, NONE, NONE), "" },
    { "same, but wifi is down",
      IN(PERSON_HI, NONE, PRESENT, NIGHT, AWAY, DOWN, NONE, NONE, NONE), "" },
    { "intruder comes back (human approves calling help)",
      IN(PERSON_HI, NONE, PRESENT, NIGHT, AWAY, UP, NONE, FEW, ALERT), "y" },
    { "loud stranger returns in daytime (human refuses alarm + help)",
      IN(PERSON_HI, LOUD, PRESENT, AFTERNOON, AWAY, UP, NONE, FEW, ALERT), "n" },
    { "night visitor while a courier is expected (spoofing risk)",
      IN(PERSON_HI, NONE, PRESENT, NIGHT, AWAY, UP, COURIER, NONE, NONE), "" },
};

const int dg_n_scenarios = (int)(sizeof dg_scenarios / sizeof dg_scenarios[0]);

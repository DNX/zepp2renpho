# Babies and pets

Zepp keeps family members (adults, babies, pets) as members of the owner's account. Each reading carries the id of its member, so the tool can send each member to its own RENPHO profile.

## 1. Find the Zepp members

```sh
uv run zepp-export members
```

The list shows the member id, the name and, when Zepp marks it, the type (baby or adult). The owner is always `-1`. The type in this list has no effect on the import: the RENPHO profile type decides.

Zepp keeps the birth month of each member in `members.json` of the export (field `birthday`). Use it when you create the RENPHO profile.

## 2. Create the RENPHO profiles

In the RENPHO Health app:

1. Open the home page of the scale.
2. Tap your name at the top, or tap **Settings** and then **User Management**.
3. Tap **Add a member**.
4. Tap **Add Baby User** for a baby or a pet. Tap the normal member type for an adult.
5. Enter the name, the birth date and the sex, then tap **Confirm and add**.

The names of the menu items can be different in your app language or version.

Then run `uv run renpho-import members` and write the new profile ids in `mapping.json`.

## 3. Give each profile a template

For each profile, the tool copies the scale fields (device type, scale address) from the newest reading of that profile. A new profile has no reading. You have two choices.

### Choice A: one real weigh-in (recommended)

Weigh the member once on the new scale. For a baby or a pet, use **Baby weighing** in the app: you step on the scale alone, then again with the baby or the pet in your arms. RENPHO does not weigh a baby below 2 kg.

After the sync, `members` shows `template ok` for that profile.

### Choice B: borrow the owner's template

Add `--borrow-owner-template` to `plan`, `test` and `import`. For a profile without readings, the tool then:

- copies the scale fields from the owner's newest reading;
- takes the sex, the birth date, the height and the profile type from the RENPHO profile;
- sends the weight only.

This choice was tested on three baby profiles. Because RENPHO does not document it, always run `test` for each member first and examine the reading in the app:

```sh
uv run renpho-import test --export $M --mapping $M/mapping.json \
  --account-id $ACC --member 1500000000001 --borrow-owner-template
```

The reading must show on the correct profile, with the correct date and weight, and not on your own profile.

## What a baby or pet reading contains

The tool sends the weight only for a baby profile, also if Zepp has more values. RENPHO calculates no body composition for babies.

Zepp often keeps adult values in a pet's member profile (for example the height of the owner). The tool takes the person fields from the RENPHO profile, not from Zepp, so these values do not move to RENPHO.

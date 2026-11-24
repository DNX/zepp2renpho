# zepp2renpho

Move your smart scale history from **Zepp** (Amazfit, Zepp Life, ex Mi Fit) to **RENPHO Health**, family members, babies and pets included.

RENPHO Health has no import feature. It writes to Health Connect, but it does not read from it. This tool reads your history from the Zepp cloud and writes it to the RENPHO cloud, one profile at a time, and reads every reading back to verify it.

> **Unofficial.** This project has no link to Zepp Health, Huami, Xiaomi or RENPHO. It uses private cloud APIs that can change at any time. Use it at your own risk and only with your own accounts.

## What the tool transfers

| Data | Transferred |
|---|---|
| Weight, BMI | Yes |
| Body fat %, body water %, muscle mass, bone mass, BMR, visceral fat | Yes, when the old scale measured them |
| Protein %, metabolic age | Yes, when the old scale measured them |
| Family members (adults, babies, pets) | Yes, one RENPHO profile for each Zepp member |
| Subcutaneous fat, skeletal muscle, fat-free mass | No. Zepp does not measure them |
| Rows that Zepp copied from other apps (for example Google Fit) | No. See [How it works](docs/how-it-works.md) |

The original date and time of each reading stay the same, with the correct daylight saving time.

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- Your Zepp account. See [Access to your accounts](docs/access.md).
- Your RENPHO Health account with an email and a password. If you sign in with Google, set a password first. See [Access to your accounts](docs/access.md).
- At least one reading on the new RENPHO scale for your own profile.

## Install

```sh
git clone https://github.com/WaYdotNET/zepp2renpho.git
cd zepp2renpho
uv sync
```

## Procedure

The procedure has three parts: export from Zepp, prepare RENPHO, import. Each upload command asks you to type `UPLOAD` before it sends data.

### 1. Export from Zepp

If you sign in to Zepp with email and password:

```sh
ZEPP_EMAIL=you@example.com ZEPP_PASSWORD='...' uv run zepp-export --out exports/main
```

If you sign in with Google, Apple or Xiaomi, or if the password login fails, use a token. [Access to your accounts](docs/access.md) tells you where to find it.

```sh
ZEPP_APP_TOKEN='...' ZEPP_USER_ID=1234567890 \
  uv run zepp-export --out exports/main --api-host api-mifit-de2.zepp.com
```

The command prints one line for each member, with the number of readings and the first and last date. If the last date is old, you are on the wrong Zepp account. See [More than one Zepp account](docs/access.md#more-than-one-zepp-account).

### 2. Prepare RENPHO

```sh
export RENPHO_EMAIL=you@example.com
uv run renpho-import members
```

The command asks for the RENPHO password. Then it prints the account id and every profile. To skip the password prompt, set `RENPHO_PASSWORD`.

1. For each baby or pet, create a profile in the RENPHO app. See [Babies and pets](docs/family-members.md).
2. Write the mapping file:

   ```sh
   uv run renpho-import mapping-template --export exports/main --mapping exports/main/mapping.json
   ```

3. Open `exports/main/mapping.json`. For each Zepp member id, write the RENPHO profile id that `members` printed. If you leave a value empty, the tool skips that member.

   ```json
   {
     "-1": "6100000000000000001",
     "1500000000001": "6100000000000000002"
   }
   ```

   In Zepp, `-1` is always the account owner.

### 3. Import

Set two shell variables to keep the commands short:

```sh
M=exports/main; ACC=6100000000000000001   # ACC is the account id from `members`
```

1. Run the plan. It reads only and sends nothing.

   ```sh
   uv run renpho-import plan --export $M --mapping $M/mapping.json --show-payload 2
   ```

   For each member, the plan shows the readings that match, the readings that are missing and the conflicts. A conflict is a reading at the same second with different values. The tool does not upload while a conflict remains.

2. Upload one reading for each member and read it back:

   ```sh
   uv run renpho-import test --export $M --mapping $M/mapping.json --account-id $ACC --member=-1
   ```

   Open the RENPHO app and examine that reading: the profile, the date, the weight and the body values. If a value is wrong, delete the reading in the app and [open an issue](https://github.com/WaYdotNET/zepp2renpho/issues).

3. Repeat the test for each family member. Use the Zepp member id in `--member`. For a profile without readings, add `--borrow-owner-template`. See [Babies and pets](docs/family-members.md).

4. Import the rest:

   ```sh
   uv run renpho-import import --export $M --mapping $M/mapping.json --account-id $ACC --borrow-owner-template
   ```

   The tool sends 10 readings at a time, newest first, and reads each group back. About 1400 readings take 25 to 30 minutes. To start small, add `--limit 20`.

If a command stops, read its message, then run `plan` again. The tool skips the readings that are already in RENPHO, so you can run `import` again without duplicates. See [Troubleshooting](docs/troubleshooting.md).

## Commands

| Command | Purpose |
|---|---|
| `zepp-export [--out DIR]` | Write `members.json` and `records.jsonl` for the owner and all family members |
| `zepp-export members` | Print the Zepp members only |
| `renpho-import members` | Print the RENPHO account id and profiles |
| `renpho-import mapping-template` | Write an empty mapping file |
| `renpho-import plan` | Compare the export with RENPHO. Sends nothing |
| `renpho-import test` | Upload one reading of one member and verify it |
| `renpho-import import` | Upload all missing readings and verify them |

Run any command with `--help` for all options. Both tools accept a debug option (`--debug` for `zepp-export`, `--debug DIR` for `renpho-import`) that writes the raw API answers to disk with the tokens removed.

## Environment variables

| Variable | Used by | Content |
|---|---|---|
| `ZEPP_EMAIL`, `ZEPP_PASSWORD` | `zepp-export` | Zepp email login |
| `ZEPP_APP_TOKEN`, `ZEPP_USER_ID` | `zepp-export` | Zepp token login |
| `ZEPP_API_HOST` | `zepp-export` | Zepp data host, for example `api-mifit-de2.zepp.com` |
| `RENPHO_EMAIL`, `RENPHO_PASSWORD` | `renpho-import` | RENPHO login |

## Documentation

- [Access to your accounts](docs/access.md): where to find the Zepp token, the data host and the RENPHO password.
- [Babies and pets](docs/family-members.md): RENPHO profiles for family members.
- [How it works](docs/how-it-works.md): data flow, field mapping, skipped rows, safety checks.
- [Troubleshooting](docs/troubleshooting.md): error messages and what to do.

## Privacy

- The tool talks only to the Zepp and RENPHO servers. It sends no data to other places.
- Your export files are health data. `.gitignore` keeps `exports/`, `*.jsonl`, `mapping.json` and debug folders out of git. Do not share them.
- A Zepp token gives access to your Zepp account for about 30 days. Do not paste it in public places.

## Tested with

| Side | Version |
|---|---|
| Zepp | Two accounts on the EU data host (token login), owner and three family members, about 1400 readings |
| RENPHO | RENPHO Health on Android with an ES-32MD scale (`deviceType 00048`), adult profile and three baby profiles |

Other scale models will probably work, because the tool copies the scale fields from your own newest reading. Run `test` first.

## Credits

- [renpho-py](https://github.com/ChocoTonic/renpho-py) for the RENPHO login, encryption and history reads.
- [renpho-history-import](https://github.com/dchapman992000/renpho-history-import) for the RENPHO upload route.
- [huami-token](https://github.com/argrento/huami-token), [zepp-health-cli](https://github.com/m4ary/zepp-health-cli) and [SmartScaleConnect](https://github.com/AlexxIT/SmartScaleConnect) for the Zepp API.

## License

[MIT](LICENSE)

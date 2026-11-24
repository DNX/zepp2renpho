# Troubleshooting

If a command stops, read the last message first. Most messages tell you the next step. Before you start an upload again, run `plan`.

## Zepp export

| Symptom | Cause | Action |
|---|---|---|
| `the app token is not valid any more (HTTP 401)` | The token is wrong or expired | Get a new `apptoken`. See [Access](access.md#option-b-token-from-the-zepp-web-page) |
| The export finishes with 0 readings | Wrong data host | Set `--api-host` to the value of your account. See [The data host](access.md#the-data-host) |
| Only the owner, no family members | Wrong account, or no members in that account | Sign in with the other method. See [More than one Zepp account](access.md#more-than-one-zepp-account) |
| The last reading is years old | Wrong account | Same as above |
| The password login fails | Zepp changed the login flow | Use the token login |
| `routes not found` | Zepp retired an API route | Run with `--debug` and open an issue with the file names in `debug/` (not their content) |

## RENPHO login

| Message | Cause | Action |
|---|---|---|
| `code=106, msg=user does not exist` | The email is not a RENPHO Health account | Use the email from the app: **Me**, **Settings**, **Account** |
| `code=104, msg=wrong user name or password` | Wrong password, or Google sign-in | Set a password. See [Access](access.md#if-you-sign-in-to-renpho-with-google) |
| `Login account mismatch` | `--account-id` is not the signed-in account | Use the account id from `members` |

## Plan

| Message | Cause | Action |
|---|---|---|
| `Skipped <name> ... no RENPHO profile in the mapping file` | Empty value in `mapping.json` | Write the profile id, or leave it empty to skip that member |
| `RENPHO profile <id> is not on this account` | Wrong id in `mapping.json` | Copy the id again from `members` |
| `template MISSING` | The RENPHO profile has no reading | Weigh the member once, or use `--borrow-owner-template`. See [Babies and pets](family-members.md) |
| `conflict <time> (<fields>)` | RENPHO has a different reading at the same second | Examine that reading in the app. If it is a wrong test reading, delete it. If both are real, remove the line from `records.jsonl` |
| `Two different exported readings share timestamp` | The export has two rows at the same second | Remove one of the two lines from `records.jsonl` |

## Upload

| Message | Cause | Action |
|---|---|---|
| `no imported reading has been verified yet` | No `test` for this profile | Run `test --member <zepp id>` and examine the reading in the app |
| `Readback could not verify <time>` | RENPHO did not store the reading as sent, or stored it on a different profile | Look for that reading in the app. Delete it if it is wrong, then run `plan` |
| `RENPHO rejected the upload (code ...)` | RENPHO refused the data | Run `test` with `--debug DIR` and open an issue with the code and the message |
| `code 429` or HTTP 429 | Too many requests | Wait. If the message gives a time, wait at least that time. Then run `plan` and `import` again |
| `-114 (request decryption failed)` | RENPHO changed the app protocol | Open an issue. Do not try again in a loop |
| `Stopped before uploading.` | You did not type `UPLOAD` | Run the command again |

## Interrupted import

An interrupted group can be partly saved. Run `plan`: the readings that RENPHO already has show as matching, and `import` skips them. Run `import` again with the same options.

## Undo

The tool cannot delete readings. To remove imported readings, delete them in the RENPHO app.

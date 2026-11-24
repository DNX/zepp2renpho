# Access to your accounts

The tool needs access to two cloud accounts: Zepp (to read) and RENPHO Health (to write). This page tells you where to find each value.

## Zepp

Zepp, Zepp Life and the old Mi Fit app use the same cloud (Huami). The tool accepts two types of login.

### Option A: email and password

This option works only if you sign in to the Zepp app with an email and a password.

```sh
ZEPP_EMAIL=you@example.com ZEPP_PASSWORD='...' uv run zepp-export --out exports/main
```

The login answer gives the correct data host, so `--api-host` is not necessary.

If Zepp rejects the login, use option B. Zepp changes the login flow from time to time.

### Option B: token from the Zepp web page

Use this option if you sign in with Google, Apple, Xiaomi or WeChat, or if option A fails. This option was tested.

1. On a computer, open https://user.huami.com/privacy2/index.html.
2. Sign in with **the same method** that you use in the Zepp app. Each sign-in method makes a different account (see [More than one Zepp account](#more-than-one-zepp-account)).
3. Open the developer tools of the browser (F12 in Chrome, Edge and Firefox).
4. Go to **Application** (Chrome, Edge) or **Storage** (Firefox), then **Cookies**, then `https://user.huami.com`.
5. Copy the value of the cookie `apptoken`. This is a long text of about 150 characters.
6. Copy the value of the cookie `userid`. This is a number.
7. Find the data host. A cookie on the same page contains a list of hosts (URL encoded). Find the entry with `"host":"api-mifit.huami.com"` and copy its `cnames` value, for example `api-mifit-de2.zepp.com` for Europe.

Then run:

```sh
ZEPP_APP_TOKEN='<apptoken>' ZEPP_USER_ID=<userid> \
  uv run zepp-export --out exports/main --api-host api-mifit-de2.zepp.com
```

If the host is wrong, the export finishes with zero readings. If the token is wrong or expired, the export stops with "the app token is not valid any more (HTTP 401)".

The token stays valid for about 30 days. To cancel it, sign out on the web page.

### The data host

| Region | Host |
|---|---|
| Europe | `api-mifit-de2.zepp.com` |
| Other regions | Use the value from step 7, or the value in the login answer (`--debug`) |

You can also set the host with the `ZEPP_API_HOST` environment variable.

### More than one Zepp account

Each sign-in method (email, Google, Apple, Xiaomi) makes a separate Zepp account with a separate history. If the export shows only some of your readings, or if the last date is old, you used a different account with the scale.

1. Export each account to its own folder, for example `exports/main` and `exports/old`.
2. Import the account with the most readings first.
3. Run `plan` on the second account. If the same weigh-in exists in both accounts with different values, the plan shows a conflict. Remove those readings from the second `records.jsonl` (one JSON object for each line), or keep them out of the import with an empty mapping.
4. Run `test` and then `import` on the second account.

### Why not the official Zepp data export

The privacy page (https://user.huami.com/privacy/index.html) can send you a ZIP with CSV files. This export is not sufficient:

- the `BODY` file contains no member id, so baby and pet readings are mixed with yours;
- the file stops at 1000 rows.

The API used by `zepp-export` gives the member of each reading and has no row limit.

## RENPHO Health

The tool signs in with the email and the password of the RENPHO Health app.

RENPHO has no web site for your health data. https://renpho.com/account/login is the account of the online shop, which is a different account.

### If you sign in to RENPHO with Google

A Google sign-in has no RENPHO password. Set one:

1. In the RENPHO Health app, sign out.
2. On the sign-in screen, tap "Forget password?".
3. Type the email of your Google account and enter the code that RENPHO sends you. The code stays valid for 30 minutes.
4. Set a new password with 6 to 16 letters and numbers.
5. Sign in to the app with email and password. Make sure that you see your readings. If the app is empty, you are on a different account. Stop and correct this before you import.

### Check the login

```sh
export RENPHO_EMAIL=you@example.com
uv run renpho-import members
```

| Message | Cause |
|---|---|
| `code=106, msg=user does not exist` | The email is not a RENPHO Health account |
| `code=104, msg=wrong user name or password` | Wrong password, or the account uses Google sign-in |

If a password contains `$`, `!` or `'`, do not put it in `RENPHO_PASSWORD` on the command line. Let the command ask for it.

The account id that `members` prints is necessary for `test` and `import` (`--account-id`). The tool compares it with the signed-in account, so that it cannot write to a different account by mistake.

# HubSpot + Scriptius Integration Guide

Connect HubSpot deals to Scriptius so client data auto-fills before each call.

---

## How it works

1. You open a Deal in HubSpot
2. Copy the deal URL from your browser
3. Paste it into the **HubSpot Deal** field in Scriptius
4. Click the arrow button (or press Enter)
5. Client name, role, company, and industry fill in automatically from HubSpot

---

## Setup (one-time, ~10 minutes)

### Step 1: Create a Private App in HubSpot

1. Go to **Settings** (gear icon, top right)
2. In the left sidebar: **Integrations** → **Private Apps**
3. Click **"Create a private app"**
4. Fill in:
   - **Name**: `Scriptius`
   - **Description**: (optional) `Sales call assistant integration`
5. Go to the **Scopes** tab
6. Search and add these scopes:
   - `crm.objects.deals.read`
   - `crm.objects.contacts.read`
7. Click **"Create app"**
8. A dialog shows your **Access Token** — copy it and save it somewhere safe

> You'll need this token in Step 2. You can always find it later in the Private App settings.

### Step 2: Add the token to Scriptius

Open the file `server/.env` on your Scriptius server and add:

```
HUBSPOT_ACCESS_TOKEN=pat-eu1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Replace with the token you copied in Step 1.

Then restart the Scriptius server:
```bash
cd server
pkill -f "uvicorn main:app"
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Step 3: Done!

That's all the setup needed. Now you can use it.

---

## Daily Usage

### Loading client data from a deal

1. Open the deal in HubSpot
2. Copy the URL from your browser bar, e.g.:
   ```
   https://app-eu1.hubspot.com/contacts/148211401/record/0-3/498351163593/
   ```
3. In Scriptius, paste the URL into the **"HubSpot Deal"** field (top of the client card)
4. Press **Enter** or click the **arrow button**
5. The client card fills with data from HubSpot:
   - Name
   - Position (job title)
   - Company
   - Industry

> You can also paste just the deal ID number (e.g. `498351163593`) instead of the full URL.

### Starting the call

1. Load the deal data (steps above)
2. Select the **Course** from the dropdown
3. Click **"Start Call"**
4. The call begins with the client card already filled

---

## What data gets pulled from HubSpot

Scriptius reads data from the **Contact** associated with the Deal:

| HubSpot Contact Field | Scriptius Client Card |
|---|---|
| First name + Last name | **Name** |
| Job title | **Position** |
| Company name | **Company** |
| Industry | **Industry** |
| Phone / Mobile phone | (stored internally) |

And from the Deal itself:

| HubSpot Deal Field | Scriptius |
|---|---|
| Deal name | (stored internally for reference) |

> Fields that are empty in HubSpot will stay empty in Scriptius. You can fill them manually during the call, or the AI will extract them from the conversation.

---

## Tips

- **Fill your HubSpot contacts**: The more data you have in HubSpot (job title, company, industry), the more Scriptius can pre-fill. Make sure contacts on your deals have at least a name and company.

- **One contact per deal**: Scriptius loads the first contact associated with the deal. If a deal has multiple contacts, it picks the first one.

- **Data stays for the session**: Once loaded, the client data stays until you start a new call or refresh the page.

- **Works with any HubSpot account**: Each account just needs its own Private App with the token configured on the Scriptius server.

---

## Troubleshooting

### "!" button (error) when loading a deal

- **Check the deal ID**: Make sure you pasted a valid HubSpot deal URL or number
- **Check the token**: Verify `HUBSPOT_ACCESS_TOKEN` is set correctly in `server/.env`
- **Check scopes**: The Private App needs `crm.objects.deals.read` and `crm.objects.contacts.read`
- **Check the server logs**: Look at the terminal where Scriptius is running for error messages

### Client card only shows name, missing other fields

- The contact in HubSpot doesn't have those fields filled in (job title, company, industry)
- Go to the contact in HubSpot and add the missing info

### No contact data at all

- The deal might not have an associated contact
- Open the deal in HubSpot → make sure a Contact is linked to it

### Token expired or invalid

- Go to HubSpot **Settings** → **Private Apps** → **Scriptius** → rotate/copy the token
- Update it in `server/.env` and restart the server

---

## For deployment (Fly.io)

When running Scriptius on Fly.io instead of localhost:

1. Set the token as a Fly secret:
   ```bash
   fly secrets set HUBSPOT_ACCESS_TOKEN=pat-eu1-xxxxxxxx
   ```

2. Deploy:
   ```bash
   cd server
   fly deploy
   ```

3. Use the same workflow — paste HubSpot deal URLs into Scriptius at `https://scriptius.fly.dev`

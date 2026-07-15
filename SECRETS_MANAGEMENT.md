# 🔐 Ivy Gateway — Secure Secrets Management Guide

This document covers **best practices** for storing sensitive credentials and API keys for your Ivy Local Admin API Gateway.

---

## ⚠️ CRITICAL: What NOT to Do

❌ **Never commit these to GitHub:**
- `.env` files
- `service-account-key.json` (GCP private keys)
- API keys, tokens, or credentials of any kind
- `discord_backup_codes.txt` or recovery codes
- Phone numbers or personal contact info

❌ **Never hardcode secrets** in `main.py`, `config.py`, or any source files

❌ **Never share secrets** in Slack, email, or unencrypted channels

---

## ✅ Recommended: Where to Store Secrets Properly

### **1. macOS Keychain (LOCAL DEVELOPMENT - RECOMMENDED FOR YOU)**

**Best for:** Local development on a single Mac (your setup)

**Advantages:**
- ✅ Built into macOS
- ✅ Encrypted at rest
- ✅ No external dependencies
- ✅ Easy to access programmatically
- ✅ Zero exposure risk (never leaves your machine)

**Setup:**

```bash
# Store API key in Keychain
security add-generic-password -s "ivy-gemini-key" -a "admin" -w "your_actual_api_key_here"

# Store DeepSeek key
security add-generic-password -s "ivy-deepseek-key" -a "admin" -w "your_actual_deepseek_key_here"

# Store H-E-B credentials
security add-generic-password -s "ivy-heb-username" -a "admin" -w "your_heb_email@example.com"
security add-generic-password -s "ivy-heb-password" -a "admin" -w "your_heb_password"
```

**Retrieve in Python:**

```python
import subprocess

def get_keychain_secret(service: str, account: str = "admin") -> str:
    """Retrieve secret from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""

# In your .env or startup:
os.environ["GEMINI_API_KEY"] = get_keychain_secret("ivy-gemini-key")
os.environ["DEEPSEEK_API_KEY"] = get_keychain_secret("ivy-deepseek-key")
os.environ["HEB_USERNAME"] = get_keychain_secret("ivy-heb-username")
os.environ["HEB_PASSWORD"] = get_keychain_secret("ivy-heb-password")
```

**File: `.gitignore` — already exclude `.env`:**

```
.env
.env.local
.env.*.local
service-account-key.json
discord_backup_codes.txt
```

---

### **2. 1Password Secrets Automation (ENTERPRISE/PRODUCTION)**

**Best for:** Production, team collaboration, compliance requirements

**Advantages:**
- ✅ Zero-knowledge encryption (even 1Password staff can't see secrets)
- ✅ Audit logs (track who accessed what, when)
- ✅ Version history and rotation support
- ✅ Integrates with GitHub Actions, Docker, CI/CD
- ✅ HIPAA, SOC 2, ISO 27001 compliant
- ✅ Supports teams and permission scoping

**Setup:**

1. **Sign up:** https://1password.com (Business plan)
2. **Create a vault** for "Ivy Gateway Secrets"
3. **Add secrets as items:**
   - GEMINI_API_KEY
   - DEEPSEEK_API_KEY
   - HEB_USERNAME / HEB_PASSWORD
   - READWISE_API_KEY
   - ADMIN_SECRET

4. **Integrate with your app:**

```bash
# Install 1Password CLI
brew install 1password-cli

# Login
op account add

# Load secrets into environment
eval $(op signin)
export GEMINI_API_KEY=$(op read "op://Ivy Gateway/Gemini API Key/credential")
export DEEPSEEK_API_KEY=$(op read "op://Ivy Gateway/DeepSeek API Key/credential")

# Then start your app
python main.py
```

**Reference:** https://developer.1password.com/docs/cli/

---

### **3. AWS Secrets Manager (CLOUD PRODUCTION)**

**Best for:** Running Ivy in AWS, scaling to multiple servers

**Advantages:**
- ✅ Automatic rotation support
- ✅ Fine-grained IAM access control
- ✅ Encrypts with KMS (AWS Key Management Service)
- ✅ Audit trail via CloudTrail
- ✅ Integrates with Lambda, ECS, EC2

**Cost:** ~$0.40/secret/month + API calls

**Setup:**

```bash
# Store secret in AWS Secrets Manager
aws secretsmanager create-secret \
  --name "ivy-gateway/gemini-api-key" \
  --secret-string "your_actual_api_key_here"

# Retrieve in Python
import boto3

client = boto3.client('secretsmanager', region_name='us-east-1')
response = client.get_secret_value(SecretId='ivy-gateway/gemini-api-key')
os.environ["GEMINI_API_KEY"] = response['SecretString']
```

**Reference:** https://aws.amazon.com/secretsmanager/

---

### **4. HashiCorp Vault (ADVANCED SELF-HOSTED)**

**Best for:** On-premises infrastructure, multi-region setups

**Advantages:**
- ✅ Open-source or enterprise
- ✅ Dynamic secrets (auto-rotate credentials)
- ✅ Encryption as a service
- ✅ Multi-cloud support
- ✅ Detailed audit logging

**Reference:** https://www.vaultproject.io/

---

### **5. GitHub Secrets (CI/CD ONLY)**

**Use ONLY for:** GitHub Actions workflows, not for storing local secrets

**Setup:**

1. Go to repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Add secrets:
   - `GEMINI_API_KEY`
   - `DEEPSEEK_API_KEY`
   - `ADMIN_SECRET`

**Usage in GitHub Actions:**

```yaml
name: Deploy Ivy
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Set environment variables
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: |
          python main.py
```

---

## 📋 My Recommendation for YOU (Local Development)

Given your current setup (local Mac development), here's the **ideal workflow:**

### **Option 1: Hybrid (Keychain + .env.local)**

**Most practical for your use case:**

1. **Store critical secrets in Keychain:**
   ```bash
   security add-generic-password -s "ivy-gemini" -a "admin" -w "$GEMINI_API_KEY"
   security add-generic-password -s "ivy-deepseek" -a "admin" -w "$DEEPSEEK_API_KEY"
   security add-generic-password -s "ivy-admin-secret" -a "admin" -w "your_secure_random_secret"
   ```

2. **Create `.env.local` (add to `.gitignore`):**
   ```bash
   # .env.local — NOT committed to git
   # Retrieve these from Keychain on startup
   CHAT_DB_PATH=/Users/lexi/Library/Messages/chat.db
   HEB_USERNAME=your_heb_email@example.com
   HEB_PASSWORD=from_keychain
   READWISE_API_KEY=from_keychain
   ```

3. **Modify `main.py` startup to load from Keychain:**
   ```python
   # At top of main.py after imports
   def load_keychain_secrets():
       """Load critical secrets from macOS Keychain on startup."""
       keys_to_load = {
           "GEMINI_API_KEY": "ivy-gemini",
           "DEEPSEEK_API_KEY": "ivy-deepseek",
           "ADMIN_SECRET": "ivy-admin-secret",
       }
       for env_var, keychain_service in keys_to_load.items():
           value = get_keychain_secret(keychain_service)
           if value:
               os.environ[env_var] = value
           else:
               logger.warning(f"⚠️ {env_var} not found in Keychain")
   
   load_keychain_secrets()  # Call before app startup
   ```

---

### **Option 2: Pure `.env` (Less Secure, Acceptable for LOCAL DEV)**

If Keychain feels complicated:

1. **Create `.env`** with all secrets
2. **Add to `.gitignore`:**
   ```
   .env
   .env.local
   .env.*.local
   ```
3. **Verify on every commit** that `.env` is NOT included:
   ```bash
   git status  # Should NOT show .env
   ```

**⚠️ Warning:** If someone gains access to your Mac, they'll have all secrets.

---

## 🚨 If You've Already Exposed Secrets

**Immediately:**

1. **Rotate the GCP service account key:**
   - Go to https://console.cloud.google.com/iam-admin/serviceaccounts
   - Delete the old key
   - Generate a new one
   - Update in Keychain

2. **Rotate Discord backup codes:**
   - Delete the old codes
   - Generate new ones at https://discord.com/channels/@me

3. **Rotate GitHub token (if exposed):**
   - Go to https://github.com/settings/tokens
   - Delete compromised token
   - Create a new one

4. **Commit a `.gitignore` update immediately:**
   ```bash
   git add .gitignore
   git commit -m "chore: add .env and secrets to gitignore"
   git push
   ```

---

## 📊 Comparison Table

| Solution | Local Dev | Team | Cloud | Audit | Cost | Ease |
|----------|-----------|------|-------|-------|------|------|
| **Keychain** | ⭐⭐⭐⭐⭐ | ❌ | ❌ | ✅ | FREE | ⭐⭐⭐ |
| **1Password** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | $19-30/mo | ⭐⭐⭐ |
| **AWS Secrets Manager** | ⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | $0.40/mo | ⭐⭐⭐⭐ |
| **Vault** | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | FREE/$$$ | ⭐⭐⭐⭐ |
| **.env file** | ⭐⭐ | ❌ | ❌ | ❌ | FREE | ⭐⭐⭐⭐⭐ |

---

## 🎯 Action Items for You NOW

1. **Delete exposed secrets from GitHub:**
   ```bash
   # Delete discord_backup_codes.txt from repo
   git rm discord_backup_codes.txt
   git commit -m "chore: remove exposed backup codes"
   
   # Rotate the Discord codes immediately
   ```

2. **Rotate GCP service account key:**
   - https://console.cloud.google.com/iam-admin/serviceaccounts

3. **Set up Keychain storage** (recommended):
   ```bash
   security add-generic-password -s "ivy-gemini" -a "admin" -w "$(echo $GEMINI_API_KEY)"
   security add-generic-password -s "ivy-deepseek" -a "admin" -w "$(echo $DEEPSEEK_API_KEY)"
   security add-generic-password -s "ivy-admin-secret" -a "admin" -w "your_new_random_secret"
   ```

4. **Update `.gitignore`:**
   ```bash
   echo ".env" >> .gitignore
   echo ".env.local" >> .gitignore
   echo "discord_backup_codes.txt" >> .gitignore
   echo "service-account-key.json" >> .gitignore
   git add .gitignore
   git commit -m "chore: add secrets to gitignore"
   ```

5. **Test local startup:**
   ```bash
   # Should load secrets from Keychain without needing .env file
   python main.py
   ```

---

## References

- **macOS Keychain:** `man security` or [Apple Docs](https://support.apple.com/en-us/HT204085)
- **1Password Secrets Automation:** https://developer.1password.com/docs/cli/
- **AWS Secrets Manager:** https://docs.aws.amazon.com/secretsmanager/
- **HashiCorp Vault:** https://www.vaultproject.io/docs
- **GitHub Secrets:** https://docs.github.com/en/actions/security-guides/encrypted-secrets

---

**Status:** ✅ You now have a comprehensive roadmap for secure secrets management. Start with **Keychain** for local dev, then migrate to **1Password** or **AWS Secrets Manager** when you scale beyond your Mac.

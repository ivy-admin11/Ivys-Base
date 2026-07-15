# Skill: Send iMessage
**Description:** Sends an iMessage to a phone number using the Mac's native Messages app.
**Endpoint:** POST http://localhost:8000/send_imessage
**Payload Structure:**
{
  "recipient_address": "String",
  "message_body": "String"
}
**Instructions:** Extract the recipient's phone number and the message text from the user's request. The recipient_address should be a valid phone number string (e.g. "+15551234567"). Send the payload to the endpoint and report back whether the message was sent successfully.

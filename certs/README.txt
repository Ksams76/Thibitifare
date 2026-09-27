Put Safaricom's public certificate here, named sandbox_public_key.pem
(or update DARAJA_CERT_PATH in .env to point wherever you put it).

Download it from the Daraja documentation site:
https://developer.safaricom.co.ke/Documentation
-> look for "Certificate for Testing" (sandbox) or the production
   equivalent once you go live.

This is ONLY needed if you set DARAJA_VERIFY_STUB=false in .env to use
real TransactionStatusQuery verification instead of the demo-safe stub.

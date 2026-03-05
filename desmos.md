1. Ask for a LEARCredentialMachine to your LEAR.

2. Get your dlt_address using the DOME Access Node Key Generator entering your DID key:
![DOME Access Node Key Generator home](/assets/dome_access_node_key_generator.png)
![DOME Access Node Key Generator generated keys](/assets/access_node_key_generator_output.png)

> [!CAUTION]
> Remember to securely store your keys since it can't be recovered if lost. Avoid sharing or saving it in
> insecure places, as losing it may mean losing access to important resources. Consider using a secure password manager
> or
> a dedicated secrets vault to keep it safe.

2. Register as a valid organization in the Trusted Access Node List of the corresponding environment following the instructions in the DOME Trust Framework.
Use the following YAML template as a reference:
   - name: <organization_name>
     dlt_address: <dlt_address>


3. Register as a valid service in the Trusted Services List of the corresponding environment following the instructions provided in the DOME Trust Framework. Use the following YAML template as a reference:

- clientId: "<did:key>"
  redirectUris: [ ]
  scopes: [ ]
  clientAuthenticationMethods: [ "client_secret_jwt" ]
  authorizationGrantTypes: [ "client_credentials" ]
  postLogoutRedirectUris: [ ]
  requireAuthorizationConsent: false
  requireProofKey: false
  jwkSetUrl: "https://verifier.dome-marketplace-sbx.org/oidc/did/<did:key>"
  tokenEndpointAuthenticationSigningAlgorithm: "ES256"

> [!NOTE]
> Replace <did:key> with the DID key generated in the "Desmos keys" part of the DOME Key Generator.

4. Fill the next variables in .env.desmos file:
4.1. Spring profile
Fill it the field "SPRING_PROFILES_ACTIVE" with the profile related to the environment you want to connect:
| desmos-api profiles | DOME-Gitops environments |
|:-------------------:|:------------------------:|
|         dev         |           sbx            |
|        test         |           dev            |
|        prod         |           prd            |
4.2. Operator organization identifier
Fill it the field "OPERATOR_ORGANIZATION_IDENTIFIER" with the DID you have in the LEARCredentialMachine.

4.3. External domain
Fill it the field "API_EXTERNAL_DOMAIN" with your domain url. Recommended with https.

5. Fill the next variables in .secrets.desmos:
5.1. Private key:
Fill it the field "SECURITY_PRIVATE_KEY" with the private key you receive when issuing the LEARCredentialMachine.
5.2. LEARCredentialMachine
Fill it the field "SECURITY_LEAR_CREDENTIAL_MACHINE_IN_BASE64" with your LEARCredentialMachine in base64.
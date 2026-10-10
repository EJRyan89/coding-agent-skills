# Signed download links for report exports

Status: proposed

## Context

Customers download a finished export from the reports page, which streams the file from object storage through the web service. A large export holds a web worker for the whole download, and at the end of each month downloads queue behind one another.

## Requirements

- R1. A customer downloads an export directly from object storage, with no web worker in the path.
- R2. Only a member of the export's account can obtain a link to it.
- R3. A link stops working 15 minutes after it is issued.

## Design

When a member asks to download an export, the web service checks that they belong to the export's account and returns a signed object-storage URL that expires 15 minutes later. The browser downloads the file from that URL. The service issues a new link on every request, so a member whose link expired asks again. Links are not stored.

## Alternatives considered

- Keep streaming through the web service and add workers. Rejected: the cost grows with download volume, and the queue at the end of each month remains.
- Links that never expire. Rejected: a link forwarded outside the account would grant access for as long as the export exists.

## Risks

- A link works for anyone who has it until it expires, so a link forwarded within its 15 minutes can be used outside the account. The short expiry in R3 bounds this, and it is accepted.
- If object storage rejects signed URLs, downloads fail. The streaming path stays in place behind the `signed_download_links` setting, so turning the setting off restores today's downloads.

## Rollout

The change ships behind the `signed_download_links` setting, off by default. It is enabled for internal accounts for one week, then for every account. It stores no new data, so turning the setting off rolls it back completely.

## Open questions

- Should links be issued for exports larger than 5 GB, which some browsers fail to download in one request? Owner: the reports team lead, who decides before the setting is enabled for every account.

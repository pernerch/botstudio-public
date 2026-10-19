import json
from pathlib import Path


LANG = "en"

LANGUAGES = (
    ("en", "English", "gb"),
    ("de", "Deutsch", "de"),
    ("fr", "Français", "fr"),
    ("it", "Italiano", "it"),
    ("es", "Español", "es"),
    ("nl", "Nederlands", "nl"),
    ("pl", "Polski", "pl"),
    ("cs", "Čeština", "cz"),
    ("hu", "Magyar", "hu"),
    ("si", "Slovenščina", "si"),
    ("sr", "Српски", "sr"),
    ("hr", "Hrvatski", "hr"),
    ("tr", "Türkçe", "tr"),
    ("ar", "العربية", "ar"),
    ("el", "Ελληνικά", "el"),
    ("ru", "Русский", "ru"),
    ("no", "Norsk", "no"),
    ("sv", "Svenska", "sv"),
    ("fi", "Suomi", "fi"),
    ("da", "Dansk", "da"),
)


def set_language(language: str) -> str:
    global LANG
    supported = {code for code, _label, _flag in LANGUAGES}
    LANG = language if language in supported else "en"
    return LANG

STRINGS = {
    "en": {
        "packages.title": "Local game packages",
        "packages.check_pack": "Check and pack",
        "packages.packed_tab": "Prepared packages",
        "packages.upload_packed": "Upload to GitHub",
        "packages.upload_notice": "Upload publishes the selected package, including its images and license, to the public GitHub catalog review. Do not include secrets or content you may not redistribute.",
        "packages.packed_ready": "Ready to upload",
        "packages.uploaded": "Submitted to GitHub",
        "packages.download_use": "Download and add to Studio",
        "packages.download_complete": "Package added to Studio. Existing content was preserved; duplicate names were numbered. Imported Events are disabled.",
        "packages.download_unavailable": "Download is unavailable here because safe active import is not configured.",
        "packages.pack_rights": "I have the necessary rights to publish this source, its dependencies, images and description under the selected license.",
        "packages.simple_hint": "Check and pack a source, then select its prepared package and upload. Catalog downloads are checked and added to Studio automatically.",
        "packages.prepare_open": "Prepare package for upload...",
        "packages.prepare_title": "Prepare package for ZIP upload",
        "packages.prepare_kind": "Package type",
        "packages.prepare_function": "Function",
        "packages.prepare_event": "Event",
        "packages.prepare_source": "Saved source",
        "packages.prepare_show_published": "Show already published packages (for editing)",
        "packages.prepare_catalog_checking": "Checking current GitHub package contents...",
        "packages.prepare_catalog_checked": "GitHub checked: {count} unchanged package(s) hidden.",
        "packages.prepare_catalog_not_filtered": "No sources hidden by a GitHub comparison in this view.",
        "packages.prepare_catalog_local_timeout": "Local package comparison exceeded its time budget.",
        "packages.prepare_catalog_failed": "GitHub comparison incomplete; unchecked sources remain visible: {error}",
        "packages.prepare_catalog_sign_in": "Sign in to the package publisher's GitHub account to compare and prepare publication.",
        "packages.prepare_already_published": "This package content is already published on GitHub. A version change alone is not an update.",
        "packages.prepare_name": "Package name",
        "packages.prepare_description": "Description",
        "packages.markdown_open": "Edit Markdown...",
        "packages.markdown_empty": "No package description added",
        "packages.markdown_summary": "{characters} Markdown characters, {images} image(s)",
        "packages.markdown_title": "Package description",
        "packages.markdown_edit": "Edit",
        "packages.markdown_preview": "Preview",
        "packages.markdown_placeholder": "Describe the package, usage, requirements or notes...",
        "packages.markdown_bold": "Bold",
        "packages.markdown_italic": "Italic",
        "packages.markdown_heading": "Heading",
        "packages.markdown_list": "List",
        "packages.markdown_attach": "Attach images...",
        "packages.markdown_remove": "Remove selected",
        "packages.markdown_image_invalid": "Select a supported, regular image file.",
        "packages.markdown_image_error": "Could not inspect the selected image: {error}",
        "packages.markdown_image_too_large": "Each attached image must be no larger than 20 MiB.",
        "packages.markdown_image_limit": "A package can include up to {limit} images.",
        "packages.markdown_image_working": "Validating and decoding images...",
        "packages.markdown_image_total": "Selected images exceed the 100 MiB package limit.",
        "packages.prepare_version_auto": "Automatic version; change for a new immutable release",
        "packages.prepare_preview": "Refresh preview",
        "packages.prepare_rights": "For this exact package shown, I confirm that I have the necessary rights to publish all included Functions, Events, Library files and other content under the stated license.",
        "packages.prepare_destination": "Save the upload-ready package ZIP",
        "packages.prepare_zip": "Create upload ZIP...",
        "packages.prepare_files": "Included files:",
        "packages.prepare_checksums": "SHA-256 checksums:",
        "packages.prepare_not_ready": "Not ready for upload: {reason}",
        "packages.prepare_ready": "Publication metadata is present. Rights are confirmed only after checking the box.",
        "packages.prepare_review": "Review the preview and confirm the rights for this specific package.",
        "packages.prepare_no_games": "No saved games were found.",
        "packages.prepare_no_sources": "No saved sources were found for this game and package type.",
        "packages.prepare_working": "Checking and preparing the package in the background...",
        "packages.prepare_draft_loaded": "Source loaded. Edit the description if needed and prepare the package.",
        "packages.prepare_failed": "Could not prepare the package: {error}",
        "packages.prepare_complete": "Upload-ready package ZIP created and verified: {path}",
        "packages.local_tab": "Local packages",
        "packages.catalog_tab": "GitHub catalog",
        "packages.more_actions": "More package actions...",
        "packages.tab": "Packages",
        "packages.sources_hint": "Advanced local archive and working-copy actions. Normal catalog downloads are checked and added to Studio automatically.",
        "packages.catalog_load": "Read GitHub catalog",
        "packages.catalog_download": "Download and inspect selected package...",
        "packages.submission_check": "Check catalog submission",
        "packages.submission_send": "Submit package as GitHub pull request...",
        "packages.submission_consent": "Send these exact package bytes PUBLICLY to {repository} (repository ID {repository_id})?\n\nAccount: @{login}\nGame: {game}\nPackage: {package} {version}\nSize: {size} bytes\nSHA-256: {sha256}\nTarget: {branch} at {commit}\n\nThis creates/reuses your technical contribution fork, adds a new branch with the archive and submission metadata, and opens a pull request. For the catalog owner, the branch is created directly in the catalog. Both GitHub accounts, permissions, bytes and target are checked again before writing. Game content, license texts and metadata become public. Do not submit secrets or content you cannot redistribute.\n\nFailures can leave a fork, branch or pull request; there is no automatic retry or cleanup. No merge, release, installation or activation. Independent catalog automation is not connected yet; a pull request alone is not publication. Send?",
        "packages.submission_sent": "GitHub pull request created: {url}\nNot merged or released. Check GitHub before retrying; catalog publication automation is still pending.",
        "packages.submission_checked": "Submission preflight passed. Nothing uploaded; use the separate pull-request submission action to send. Catalog publication automation is still pending.",
        "packages.submission_summary": "Submission preflight for {repository}\nGame: {game}\nPackage: {package} {version}\nCurrent GitHub submitter: @{login} (ID {account_id})\nSHA-256: {sha256}\nPlanned catalog path: {path}\nArchive integrity, bounds, declared license/rights and publisher/account match checked. Other author/fork declarations are not authenticated. This check created no fork, pull request, upload or publication. Use the separate submission action to send. The catalog must independently validate the PR actor, exact files and immutable versions before publication.",
        "packages.catalog_source": "GitHub catalog: {repository} (repository ID {repository_id})",
        "packages.catalog_rules": "Package assets in up to 30 latest published releases. Explicit download requires a GitHub SHA-256 digest, unchanged repository/asset metadata and a new local target file. Size, ZIP contents and package checksums are inspected before saving. Managed storage retains a hash-bound download receipt and blocks implicit repository/source changes. Reopening the saved ZIP alone does not restore this receipt. No automatic storage, activation or update. GitHub API uploaders are not verified original authors or package maintainers. Release tags are not validated package versions. Public access only; missing/private repositories and API limits are errors.",
        "packages.source_local": "Local archive: no retained GitHub download receipt. Publisher declarations are unverified.",
        "packages.source_receipt": "Download receipt: {repository} (repository ID {repository_id}), release {release_id}, asset {asset_id}. GitHub asset uploader: @{login} ({account_id}). Evidence from the checked download, not a live availability check or verified creator/contributor/update authorization. Repository replacement and implicit local/catalog source changes are blocked; publisher declarations remain unverified.",
        "packages.catalog_entry": "{name}\nRelease tag: {tag} {prerelease}\nSize: {size} bytes | Uploader: @{login} (GitHub ID {account_id})\nRelease ID: {release_id} | Asset ID: {asset_id}",
        "packages.catalog_tree": "Type > Game > Package > Publisher > Version - select a version to download",
        "packages.catalog_more": "Load older releases",
        "packages.catalog_search": "Search loaded catalog entries...",
        "packages.catalog_type_all": "All types",
        "packages.catalog_type_function": "Function",
        "packages.catalog_type_helper": "Helper",
        "packages.catalog_type_event": "Complete Event",
        "packages.catalog_type_unknown": "Unknown type",
        "packages.catalog_scope": "{matches} matches among {loaded} loaded package assets; pages loaded: {pages}. Search does not fetch more releases.",
        "packages.catalog_page_changed": "The catalog changed while loading older releases. Reload the catalog before continuing; the existing view was retained.",
        "packages.catalog_auth_failed": "GitHub catalog HTTP 401: the saved sign-in was rejected. Check or renew your GitHub sign-in under Telegram/GitHub. No anonymous retry was made.",
        "packages.catalog_access": "Catalog API reads use an existing Studio GitHub sign-in, otherwise the shared anonymous quota. No new permissions. Redirected asset hosts never receive the token.",
        "packages.catalog_credentials_failed": "Could not read a valid saved GitHub sign-in for the catalog. Check your sign-in under Telegram/GitHub. No anonymous fallback was made.",
        "packages.catalog_rate_limit_at": "GitHub request limit reached. Retry no earlier than {when} (local time). No automatic retry. Without sign-in, the small public-IP quota is shared; an existing Studio sign-in is used for catalog API reads.",
        "packages.catalog_rate_limit": "GitHub request limit reached. GitHub did not provide a valid retry time. Try again later. No automatic retry. A Studio GitHub sign-in provides a separate API quota.",
        "packages.catalog_unclassified": "Without structured publication metadata",
        "packages.catalog_declared": "Declared Game: {game}\nPackage: {package} | Version: {version}\nPublisher: @{login} (GitHub ID {account_id})\nSeparate publisher channels are alternatives, not automatic replacements. Grouping is not proof of authorship or package functionality. Archive identity is checked again on download.",
        "packages.catalog_prerelease": "(prerelease)",
        "packages.catalog_empty": "No published .botstudio.zip package assets found in these releases.",
        "packages.catalog_loaded": "{count} GitHub catalog assets read. Local packages unchanged.",
        "packages.notice": "Storing/updating an archive does not activate its contents. Use the separate activation action to add content to the Studio. Integrity reflects the last check. Publisher identities and fork references are declared, not verified; matching IDs do not authorize updates. Only use sources you trust.",
        "packages.activate": "Activate selected working copy...",
        "packages.active_update": "Update active content...",
        "packages.active_state": "Active content (last check)",
        "packages.active_not_active": "Not activated",
        "packages.active_unchanged": "Unchanged",
        "packages.active_modified": "Locally changed - active update blocked",
        "packages.active_update_available": "Newer stored version available",
        "packages.active_update_summary": "Update active {package} from {previous} to {version}?\nFunctions: {functions}\nLibrary entries: {library}\nEvents: {events}\nRetained local profile: {profile}",
        "packages.active_update_rules": "Only unchanged active content from this exact package can be replaced by a higher stored version. Local Function/Library/Event/definition edits block the update, including changes made by execution. Existing Events keep their local profile and remain disabled. Foreign profile values are never copied. Runtime/debug must be stopped and Function saves finished. Content removal, Function-interface/definition migrations and automatic merging are not supported. Declared identity does not verify the publisher.",
        "packages.active_update_complete": "Active package updated. Events remain disabled; review before enabling. Local profile values were preserved.",
        "packages.profile_required": "Create a local profile for this package's game before importing its Events.",
        "packages.target_profile": "Choose the local target profile. Imported Events stay disabled.",
        "packages.activation_summary": "Add {package} {version} to active Studio content?\nFunctions: {functions}\nLibrary entries: {library}\nEvents: {events}\nLocal profile: {profile}",
        "packages.activation_modified": "This working copy has local modifications. The activation uses these edited contents, not only the original archive.",
        "packages.activation_rules": "Runtime and debug runs must be stopped. Existing Function/Library/Event IDs and conflicting variable definitions block activation. Events are imported disabled with cleared execution state. Foreign profile values are never copied. The game must already exist locally. No automatic execution, source verification or active-version replacement is included.",
        "packages.activation_complete": "Package content added to Studio. Imported Events are disabled; review them in the Scheduler before enabling. Updating a stored archive does not update this active copy.",
        "packages.package": "Package",
        "packages.version": "Version",
        "packages.publisher": "Publisher (unverified)",
        "packages.state": "Working copy",
        "packages.refresh": "Refresh integrity",
        "packages.inspect": "Inspect package file...",
        "packages.inspect_original": "Preview selected original",
        "packages.open_working": "Open working folder",
        "packages.open_original": "Open original archive",
        "packages.preview_hint": "Inspect a local package to read its declared identity, terms and contents before storing it.",
        "packages.install": "Store as separate package",
        "packages.update": "Update stored archive...",
        "packages.failed": "Package operation failed: {error}",
        "packages.busy": "Checking package files...",
        "packages.damaged": "Damaged / unavailable",
        "packages.modified": "Locally modified - update blocked",
        "packages.clean": "Unchanged working copy",
        "packages.loaded": "{count} local packages checked.",
        "packages.preview_ready": "Preview checked. Review the declared terms before confirming. Legacy packages without publisher metadata can be inspected, but cannot be stored as managed packages.",
        "packages.confirm_install": "Store the previewed archive as a separate managed package? Review the license and third-party terms in the preview. The original archive and a separate working copy will be preserved.",
        "packages.confirm_update": "Replace the managed working copy of {package} {version} with the previewed version? Only a higher version with the same declared publisher/package/game identity and no local edits is accepted. Previous generations are retained.",
        "packages.open_failed": "The operating system could not open this location.",
        "function.checking_identity": "Checking GitHub account and saving...",
        "function.identity_check_failed": "Function not saved: {error}. Check GitHub sign-in under Telegram/GitHub, or sign out locally to save without account attribution.",
        "function.saved": "Function saved.",
        "function.creator_account": "Creator: @{login} (GitHub ID {account_id})",
        "function.revision_account": "Revision {revision} by: @{login} (GitHub ID {account_id})",
        "function.account_attribution_notice": "Stored account attribution, not a live sign-in or proof of copyright.",
        "github.account": "GitHub account",
        "github.client_id": "OAuth client ID",
        "github.client_id_hint": "Public client ID of your registered OAuth app (device flow enabled)",
        "github.local_only": "No GitHub account checked. Local work remains available.",
        "github.sign_in": "Sign in",
        "github.check_saved": "Check saved sign-in",
        "github.sign_out": "Remove local sign-in",
        "github.open_browser": "Open GitHub",
        "github.permissions_hint": "Normal sign-in requests no repository scopes. Optional publication authorization below separately requests public_repo. Tokens are stored only in Windows Credential Manager. Removing a local token does not revoke the app on GitHub. Function saves check normal sign-in again. Explicit package pull-request submission is under Games > Packages; automatic catalog publication is still pending.",
        "github.publication_unchecked": "No publication authorization checked. Normal sign-in remains separate.",
        "github.publication_authorize": "Authorize public GitHub contributions...",
        "github.publication_check": "Check publication authorization",
        "github.publication_remove": "Remove local publication token",
        "github.publication_connected": "Publication grant checked: @{login} (GitHub ID {account_id}), public_repo. Last check only; no upload performed. This account does not replace normal sign-in.",
        "github.publication_consent": "Authorize GitHub public_repo permission for future catalog forks and pull requests?\n\nIMPORTANT: This grants read/write access to PUBLIC repositories you can access, not only pernerch/botstudio-catalog. GitHub OAuth cannot restrict this scope to the catalog. No private-repository or workflow scope is requested. The token is stored separately in Windows Credential Manager; normal sign-in is not replaced. GitHub-side app grants/revocation can affect both local sign-ins.\n\nThis step creates no fork, pull request or upload. Removing the local token does not revoke GitHub authorization. Continue to GitHub's device authorization?",
        "github.busy": "Contacting GitHub...",
        "github.code": "Enter {code} at {url}. Never enter a code you did not request.",
        "github.device_code": "GitHub device code",
        "github.code_instructions": "GitHub opens in your browser. Copy this code and enter it at https://github.com/login/device to confirm your sign-in. This window closes automatically when sign-in completes. Only confirm codes you requested yourself.",
        "github.copy_code": "Copy code",
        "github.code_copied": "Code copied",
        "github.connected": "Account checked: @{login} (GitHub ID {account_id}). This is a result of the last check, not a live connection.",
        "github.package_license": "Default package license: {license}",
        "github.package_license_unset": "No default package license selected for this account.",
        "github.package_license_choose": "Choose package license...",
        "github.package_attribute_legacy": "Select existing Functions to attribute...",
        "package_license.title": "Default license for this GitHub account",
        "package_license.no_account": "Connect a GitHub account to prepare package metadata.",
        "package_license.load_failed": "Could not read the saved package license preference: {error}",
        "package_license.save_failed": "Could not save the package license preference: {error}",
        "package_license.scan_failed": "Could not read existing Functions: {error}",
        "package_license.attribute_failed": "Could not attribute the selected Functions: {error}",
        "package_license.intro": "This is only the default license for packages prepared under this GitHub account. It does not confirm the right to publish any particular package.",
        "package_license.mixed_warning": "Software licenses are intended for code; Creative Commons licenses generally suit images and other creative works, not software. Mixed packages may contain assets with different or incompatible terms. Check every included component before publishing.",
        "package_license.select": "Select a license",
        "package_license.link": "Read the complete license text",
        "package_license.mit": "Permissive; retain the copyright and license notice. No explicit patent grant.",
        "package_license.apache": "Permissive; includes an express patent license and notice requirements.",
        "package_license.bsd": "Permissive; retain notices and do not imply endorsement.",
        "package_license.mpl": "Weak copyleft: changes to covered files remain under MPL; other files may use different terms.",
        "package_license.gpl": "Strong copyleft: distributed derivative works must provide source under GPL-3.0.",
        "package_license.lgpl": "Library-focused copyleft: modifications to covered library code stay under LGPL; linking conditions apply.",
        "package_license.cc0": "Public-domain dedication and fallback license; does not provide a software-specific patent grant.",
        "package_license.cc_by": "Creative-work license requiring attribution and marking changes; not intended for software.",
        "package_license.cc_by_sa": "Creative-work license requiring attribution and the same license for adaptations; not intended for software.",
        "package_license.legacy_intro": "Only selected saved local Functions without a stored creator account or imported-package origin will be updated. Existing creator accounts and imported package contents are never replaced. This is an attribution choice, not proof of authorship or permission to publish.",
        "package_license.legacy_empty": "No saved Functions without a creator account were found.",
        "package_license.legacy_confirm": "Assign the selected {count} Function(s) to @{login} (GitHub ID {account_id})? Their existing author text and revision numbers will remain unchanged.",
        "package_license.legacy_done": "Attributed {count} Function(s) to @{login}. This stored attribution is not proof of copyright.",
        "github.operation_failed": "GitHub operation failed. Check Windows Credential Manager access and try again.",
        "github.browser_failed": "The browser could not be opened. Use https://github.com/login/device manually.",
        "function.package_origin_unverified": "Declared package provenance (unverified; not an authorship or support guarantee):",
        "function.package_origin_entry": "{publisher} / {package} {version}\n{game} / {function}, source revision {revision}\nSource SHA-256: {sha256}",
        "startup.initialization_failed": "BotStudio could not initialize its data directory.\n\n{error}\n\nCheck the directory and write permissions, then try again.",
        "watchdog.tray.start": "Start BotStudio",
        "watchdog.tray.stop": "Stop BotStudio",
        "watchdog.tray.exit": "Exit Watchdog and BotStudio",
        "watchdog.tray.ready": "Ready",
        "watchdog.tray.recovering": "Recovering",
        "watchdog.tray.failed": "Failed",
        "watchdog.tray.running": "BotStudio is running",
        "watchdog.tray.waiting": "Waiting for a Studio start command",

        # --------------------------
        # Common
        # --------------------------

        "common.verify":
            "Verify",

        "common.teach":
            "Teach",

        "common.delete":
            "Delete",
        
        "common.add":
            "Add",

        "common.save":
            "Save",

        "common.clone":
            "Clone",

        "common.cancel":
            "Cancel",

        "common.name":
            "Name",

        "common.enabled":
            "Enabled",

        "common.confirm":
            "Confirm",

        "common.copy":
            "Copy",

        "common.close":
            "Close",

        "common.save_copy_as":
            "Save copy as",

        "common.search":
            "Search...",

        # --------------------------
        # Navigation
        # --------------------------

        "nav.game":
            "Game",

        "nav.profiles":
            "Profiles",

        "nav.learning":
            "Learning",

        "nav.scheduler":
            "Scheduler",

        "nav.settings":
            "Settings",

        "nav.telegram":
            "Telegram/GitHub",

        "nav.functions":
            "Functions",

        "nav.dependencies":
            "Dependencies",

        "nav.debug":
            "Debug",

        "debug.title":
            "Runtime Debug",

        "debug.mode":
            "Debug Mode",

        "debug.mode_tooltip":
            "Enable pacing and live observation for the running runtime. Off behaves exactly like production.",

        "debug.active":
            "Debug Mode is ON — the runtime is being observed and paced.",

        "debug.inactive":
            "Debug Mode is off. The runtime runs exactly like production.",

        "debug.speed":
            "Speed factor",

        "debug.pause":
            "Pause",

        "debug.resume":
            "Resume",

        "debug.variables":
            "Variables",

        "debug.log":
            "Run log",

        "debug.freeze":
            "Freeze in debug loop",

        "debug.unfreeze":
            "Release debug loop",

        "debug.open_in_editor":
            "Open in editor",

        "dependencies.value":
            "Value",

        "dependencies.game":
            "Game",

        "dependencies.variables":
            "Variables",

        "dependencies.graph":
            "Graph",

        "dependencies.name":
            "Name",

        "dependencies.kind":
            "Kind",

        "dependencies.type":
            "Type",

        "dependencies.used_by":
            "Used by",

        "dependencies.protected":
            "Protected (declared by functions/scheduler)",

        "dependencies.used":
            "Used",

        "dependencies.unused":
            "Unused (manually created)",

        "dependencies.delete_unused":
            "Delete unused variable",

        "dependencies.delete_unused_confirm":
            "Delete the unused variable '{name}'? Functions never reference it.",

        "dependencies.edit_variable":
            "Edit variable",

        "variables.edit_title":
            "Edit variable",

        "dependencies.reason_configure_field":
            "Declared by Configure Field",

        "dependencies.reason_event_output":
            "Event output variable",

        "dependencies.reason_task_name":
            "Task name variable",

        "dependencies.reason_scheduler":
            "Scheduler convention",

        "dependencies.unused_functions":
            "Unused functions",

        "dependencies.missing_functions":
            "⚠ Missing functions ({count})",

        "dependencies.missing_hint":
            "These functions are still used by tasks or Call Function blocks but no longer exist (deleted or recreated). Double-click a user below to open it and select the current function again. Technical IDs are written to the log.",

        "dependencies.missing_node":
            "⚠ Deleted function: {name}",

        "dependencies.missing_node_unknown":
            "⚠ Deleted function (name no longer known)",

        "dependencies.missing_unknown_name":
            "unknown name",

        "dependencies.missing_tooltip":
            "Function no longer exists – a recreated function with the same name has a new ID and must be re-linked.",

        "dependencies.missing_user_task":
            "Profile {profile} → Event '{event}' → Task '{task}'",

        "dependencies.missing_no_profile":
            "(none)",

        "dependencies.profile_disabled":
            "{profile} (disabled – hidden in scheduler)",

        "scheduler.reveal_not_found":
            "Task not shown – its profile is disabled or it was removed.",

        "dependencies.missing_user_function":
            "Call Function in '{function}'",
        "dependencies.open_function":
            "→ Open",
        "dependencies.open_function_tooltip":
            "Double-click to open this function in the function editor",
        "dependencies.open_task_tooltip":
            "Double-click to open this task in the scheduler",

        "scheduler.missing_function":
            "⚠ {count} function(s) deleted – re-assign",

        "scheduler.missing_function_tooltip":
            "The function used by this task no longer exists (deleted or recreated). Open the task and select the current function again.",

        "logs.open_target":
            "Open",

        "logs.open_target_tooltip":
            "Opens the affected task or function",

        "scheduler.event_missing_functions":
            "⚠ At least one task references a function that no longer exists.",

        "dependencies.unused_library":
            "Unused library images",

        "dependencies.preview":
            "Preview",

        "dependencies.zoom_in":
            "Zoom in",

        "dependencies.zoom_out":
            "Zoom out",

        "dependencies.zoom_fit":
            "Fit to window",

        "dependencies.delete_function":
            "Delete unused function",

        "dependencies.delete_function_confirm":
            "Delete the unused function '{name}'? No task or caller references it.",

        "dependencies.delete_library":
            "Delete unused library entry",

        "dependencies.delete_library_confirm":
            "Delete the unused library entry '{name}'? No function references it.",

        # --------------------------
        # Function Editor
        # --------------------------

        "function.palette":
            "Blocks",

        "function.palette_order_tooltip":
            "Drag blocks to reorder the palette. Drag a block to the canvas to add it.",

        "function.reset_palette_order":
            "Reset block order",

        "function.name":
            "Function name",

        "function.new":
            "New",

        "function.run":
            "Run",

        "function.stop":
            "Stop",

        "function.details":
            "Function details",

        "function.description":
            "Describe the expected behavior and outcome.",

        "function.selected_block":
            "Selected block",

        "function.block_id":
            "Block ID",

        "function.parameters":
            "Parameters",

        "function.how_to":
            "How to",

        "function.output_variable":
            "Output variable",

        "function.output_type_mismatch":
            "'{name}' is a {actual} variable; this block outputs {expected}.",

        "function.usage_none":
            "Not used by any task or function yet.",

        "function.usage_tasks":
            "{count} task(s): {names}",

        "function.usage_callers":
            "{count} caller(s): {names}",

        "function.click_target":
            "Click sets",

        "function.click_target_start":
            "Swipe start (x/y)",

        "function.click_target_offset":
            "Tap offset (relative to anchor)",

        "function.block_error":
            "Block status",

        "function.debug_block":
            "Debug block",

        "function.debug_chain":
            "Debug chain",

        "function.debug_values":
            "Debug values",

        "function.debug_values_hint":
            "These values are used only for this Function's debug runs. They start with the Configure Field defaults and never change Scheduler or profile data.",

        "function.debug_values_none":
            "This Function and its Helpers do not declare any Configure Field values.",

        "function.debug_values_ready":
            "{count} debug value(s) prepared.",

        "function.debug_collection_required":
            "Debug value '{name}' must be a JSON list.",

        "function.block_required":
            "Select a block first.",

        "function.edit_details":
            "Edit details",

        "function.debug_stopped":
            "Debug run stopped.",

        "function.profile_required":
            "Select a profile first.",

        "function.debug_started":
            "Function worker started.",

        "function.debug_start_failed":
            "Function worker could not be started.",

        "function.editor_error":
            "Editor error. The details were written to the function log.",

        "function.gate_inputs":
            "Number of inputs",

        "function.splitter_outputs":
            "Number of outputs",

        "function.negate_output":
            "Negate output",

        "function.parameter_max_iterations":
            "Max iterations",

        "function.parameter_timeout_ms":
            "Timeout (ms)",

        "function.parameter_stop_after":
            "Stop on arrival",

        "function.parameter_delay_seconds":
            "Reschedule delay (seconds)",

        "function.parameter_delay_variable":
            "Seconds variable (optional)",

        "block.swipe_search_tap.label":
            "Swipe Search + Tap",

        "block.swipe_search_tap.description":
            "Swipe until a library target is found, then tap its center plus offset.",

        "block.swipe_search_tap.how_to":
            "1. Select the search anchor.\n2. Set swipe options.\n3. Set tap offset from the found center; zero taps the center.",

        "block.search_ocr_tap.label":
            "Search OCR + Tap",

        "block.search_ocr_tap.description":
            "Find matching text and tap its center plus offset.",

        "block.search_ocr_tap.how_to":
            "1. Enter text to find.\n2. Set an optional search region.\n3. Set tap offset from the found text center.",

        "block.stop_event.how_to":
            "1. Connect the normal path through this block.\n2. Set the arrival count that triggers the stop.\n3. Choose fixed seconds or a seconds variable; reaching the limit automatically ends and reschedules the event.",

        "block.set_event_due.how_to":
            "1. Select a target event in this account.\n2. It becomes due now, for the next scheduler opportunity (without interrupting the current task).\n3. This function continues normally. Disabled, protected and running events cannot be targeted.",

        "block.reschedule_event.how_to":
            "1. Select a target event in this account.\n2. Enter a delay in seconds or select a seconds variable.\n3. The target is scheduled from now plus the delay; this function continues normally.",

        "function.event_target":
            "Target event",

        "scheduler.due_now":
            "Due now",

        "scheduler.on_profile_activation":
            "On next profile activation",

        "function.event_target_hint":
            "Choose an event name in the selected account. Names must be unique; existing event IDs remain supported.",

        "function.event_targets_loading":
            "Loading events...",

        "function.event_targets_failed":
            "Could not load events: {error}",

        "settings.language":
            "Language",

        "settings.language_restart_hint":
            "Language saved. Reopen BotStudio to apply it.",

        "function.parameter_until_variable":
            "Break when variable is true",

        "function.parameter_attempts":
            "Attempts",

        "function.parameter_delay_ms":
            "Delay between attempts (ms)",

        "function.parameter_poll_ms":
            "Poll interval (ms)",

        "function.parameter_threshold":
            "Threshold (%)",

        "function.parameter_detector":
            "Detector",

        "function.parameter_offset_x":
            "Tap offset X (px)",

        "function.parameter_offset_y":
            "Tap offset Y (px)",

        "function.parameter_operator":
            "Operator",

        "function.parameter_left":
            "Left value",

        "function.parameter_right":
            "Right value",

        "function.parameter_input":
            "Input",

        "function.parameter_expression":
            "Expression",

        "function.parameter_variable":
            "Variable",

        "function.variable_name_hint":
            "Profile variable name",

        "function.parameter_margin_seconds":
            "Margin (seconds)",

        "function.parameter_negate_output":
            "Negate output",

        "function.parameter_direction":
            "Direction",

        "function.parameter_max_swipes":
            "Max swipes",

        "function.parameter_settle_ms":
            "Settle time after swipe (ms)",

        "function.parameter_expect":
            "Expect",

        "function.parameter_language":
            "Language",

        "function.parameter_query":
            "Text to find",

        "function.parameter_function_id":
            "Function",

        "function.parameter_variable_name":
            "Variable name",

        "function.parameter_field_target":
            "Scope (Event/Task)",

        "function.parameter_variable_kind":
            "Variable kind",

        "function.parameter_value_type":
            "Value type",

        "function.parameter_description":
            "Description",

        "function.parameter_initial_value":
            "Default value",

        "function.parameter_required":
            "Required",

        "function.parameter_writable_by_functions":
            "Functions may change value",

        "function.parameter_options_source":
            "Dropdown options",

        "function.parameter_fixed_options":
            "Fixed options (JSON list)",

        "function.parameter_options_variable":
            "Collection variable",

        "variables.target_task":
            "Task",

        "variables.target_event":
            "Event",

        "variables.options_none":
            "No dropdown",

        "variables.options_fixed":
            "Fixed list",

        "variables.options_variable":
            "Collection variable",

        "function.field_declaration_error_title":
            "Invalid field declaration",

        "function.field_declaration_error":
            "The Function was not saved because a Configure Field block is invalid: {error}",

        "function.learn_missing":
            "Learn missing item",

        "function.edit_existing":
            "Edit existing item",

        "function.select_function":
            "New function",

        "function.not_found":
            "The selected function could not be loaded.",

        "function.expert_mode_required":
            "Helper functions are read only. Expert mode is required to edit them.",

        "function.variable":
            "Variable",

        "function.margin_seconds":
            "Margin (seconds)",

        "function.expression":
            "Expression",

        "function.kind_regular":
            "Function",

        "function.kind_helper":
            "Helper function",

        "function.learn_element_title":
            "Learn element for function",

        "function.missing_fields_save":
            "{fields} are missing. Save anyway?",

        "function.connect":
            "Connect outcome",

        "function.connection_created":
            "Connection created",

        "function.error_self_connection":
            "A block cannot connect directly to its own input.",

        "function.error_cycle_detected":
            "Invalid infinite loop detected. Please use a Repeat block for loops.",

        "function.disconnect":
            "Disconnect",

        "function.connections":
            "Connections",

        "function.not_connected":
            "Not connected",

        "function.incoming_connections":
            "Incoming",

        "function.outgoing_connections":
            "Outgoing",

        "function.port_break":
            "Break loop",

        "function.port_input":
            "Input",

        "function.wire_disconnected":
            "Connection removed",

        "function.wire_hint":
            "Drag wire to reconnect or drop in empty canvas to disconnect",

        "function.delete_block":
            "Delete block",

        "function.clone_block":
            "Clone blocks",

        "function.clone_block_tooltip":
            "Duplicates all marked blocks with their settings, variables and internal connections (Ctrl+D)",

        "function.capture_screen":
            "Capture live screen",

        "function.search_region":
            "OCR search region",

        "function.full_screen_region":
            "Full screen (no region selected)",

        "function.ocr_region_from_input":
            "Library boundary or incoming coordinates (no explicit region selected)",

        "function.select_region":
            "Select region",

        "function.redraw_region":
            "Redraw region",

        "function.clear_region":
            "Clear region",

        "function.drag_region":
            "Drag a rectangle on the preview",

        "function.capture_before_region":
            "Capture a screenshot before selecting a region.",

        "function.ocr_query_text":
            "Text to find on screen",

        "function.insert_variable":
            "Insert variable...",

        "function.value_mode_literal":
            "Fixed value",

        "function.value_mode_variable":
            "Variable",

        "function.region_selected":
            "OCR search region selected.",

        "function.no_live_image":
            "No live image captured",

        "function.no_library_preview":
            "No library preview available",

        "function.preview":
            "Preview",

        "function.coordinate_selected":
            "Coordinate selected",

        "function.block_type":
            "Block type",

        "function.outcome":
            "Outcome",

        "function.target":
            "Target block",

        "function.select_library":
            "Select library element",

        "function.library_entry":
            "Library element",

        "function.threshold":
            "Threshold",

        "function.left_value":
            "Left value",

        "function.operator":
            "Operator",

        "function.right_value":
            "Right value",

        "function.parameter_x":
            "X",

        "function.parameter_y":
            "Y",

        "function.parameter_start_x":
            "Start X",

        "function.parameter_start_y":
            "Start Y",

        "function.parameter_end_x":
            "End X",

        "function.parameter_end_y":
            "End Y",

        "function.parameter_duration_ms":
            "Duration (ms)",

        "function.block.find_element":
            "Find element",

        "function.block.find_and_tap_element":
            "Find and tap element",

        "function.block.tap_element":
            "Tap element",

        "function.block.wait_for_element_and_tap":
            "Wait for element and tap",

        "function.block.tap_point":
            "Tap point",

        "function.block.android_back":
            "Android back",

        "function.block.press_enter":
            "Press enter",

        "function.block_description.press_enter":
            "Send the Enter key to the device.",

        "function.block.clear_field":
            "Clear field",

        "function.block.type_text":
            "Type text",

        "function.block_description.clear_field":
            "Tap a field and delete its content via backspace.",

        "function.block_description.type_text":
            "Tap a field and type a fixed value or a variable into it.",

        "function.parameter_default_value":
            "Default value (if nothing is read)",

        "function.parameter_delay_before_read_ms":
            "Delay before OCR read (ms)",

        "function.parameter_count":
            "Characters to delete",

        "function.parameter_text":
            "Text or $variable",

        "function.parameter_tap_delay_ms":
            "Delay before tap (ms)",

        "function.parameter_delay_ms":
            "Delay before the key (ms)",

        "function.block.swipe":
            "Swipe",

        "function.block.swipe_search":
            "Swipe search",

        "function.block.swipe_search_tap":
            "Swipe Search + Tap",

        "function.block.search_ocr_tap":
            "Search OCR + Tap",

        "function.block.ocr_read":
            "Read OCR",

        "function.block.search_ocr":
            "Search OCR",

        "function.block.wait":
            "Wait",

        "function.block.wait_for_element":
            "Wait for element",

        "function.block.call_function":
            "Call function",

        "function.block.condition":
            "Condition",

        "function.block.compare":
            "Compare",

        "function.block.duration_parse":
            "Duration parse",

        "function.block.due_date_remaining":
            "Due date remaining",

        "function.block.gate":
            "Logic gate",

        "function.block.repeat":
            "Repeat",

        "function.block.stop_event":
            "Stop event",

        "function.block.set_event_due":
            "Set event due",

        "function.block.reschedule_event":
            "Reschedule event",

        "function.block.splitter":
            "Splitter",

        "function.block.timer":
            "Timer",

        "function.block.variable":
            "Variable",

        "function.block.configure_field":
            "Configure field",

        "function.block.negate_outcome":
            "Negate outcome",

        "function.block.coordinate_anchor":
            "Coordinate anchor",

        "function.block_description.coordinate_anchor":
            "Provide a fixed screen region for OCR or coordinate-aware blocks.",

        "function.block.find_color_trigger":
            "Find color trigger",

        "function.block.find_shape":
            "Find shape",

        "function.block_description.find_color_trigger":
            "Find a learned color region and return its coordinates.",

        "function.block_description.find_shape":
            "Find a learned shape using its detection profile.",

        "function.block.success_terminal":
            "End (OK)",

        "function.block.failure_terminal":
            "End (Fail)",

        "function.end_wire_ok":
            "Wire to End (OK) – deliberate clean exit",

        "function.end_wire_fail":
            "Wire to End (Fail) – deliberate failed exit",

        "function.end_wire_ok_new":
            "Wire to End (OK) – new block next to it",

        "function.end_wire_fail_new":
            "Wire to End (Fail) – new block next to it",

        "function.open_end_tooltip":
            "open end: not wired, the run ends as failed here. Right-click to wire it to an End block.",

        "function.open_ends_button":
            "Open ends: {count}",

        "function.open_ends_button_tooltip":
            "Unwired failed / not found outputs in this function. Click for an overview of all functions.",

        "function.open_ends_title":
            "Open ends per function",

        "function.open_ends_hint":
            "{total} open failure outputs in {functions} functions (helpers included). Display only: the run behaviour is unchanged. Wire deliberate exits to End (OK) / End (Fail), unexpected ones to a handling route. Double-click opens the function.",

        "function.open_ends_col_function":
            "Function",

        "function.open_ends_col_kind":
            "Kind",

        "function.open_ends_col_count":
            "Open ends",

        "function.open_ends_open":
            "Open function",

        "function.block_description.success_terminal":
            "Finish the current path successfully without an action (deliberate clean end).",

        "function.block_description.failure_terminal":
            "Finish the current path as failed without an action (deliberate failed end).",

        "function.block_description.find_element":
            "Find a learned library element in its search area.",

        "function.block_description.find_and_tap_element":
            "Find a learned library element and tap it immediately.",

        "function.block_description.tap_element":
            "Find a learned library element and tap it.",

        "function.block_description.wait_for_element_and_tap":
            "Wait until a learned element appears and then tap it.",

        "function.block_description.tap_point":
            "Tap a fixed coordinate.",

        "function.block_description.android_back":
            "Send the Android back key to the device.",

        "function.block_description.swipe":
            "Perform a swipe gesture.",

        "function.block_description.swipe_search":
            "Swipe through an area until an element is found.",

        "function.block_description.swipe_search_tap":
            "Swipe until a library target is found, then tap its center plus offset.",

        "function.block_description.search_ocr_tap":
            "Find matching text and tap its center plus offset.",

        "function.block_description.ocr_read":
            "Read text or numbers from an area.",

        "function.block_description.search_ocr":
            "Find text on the screen and return its coordinates.",

        "function.block_description.wait":
            "Wait for a fixed duration.",

        "function.block_description.wait_for_element":
            "Wait until an element appears or the timeout expires.",

        "function.block_description.call_function":
            "Call a sealed helper function.",

        "function.block_description.condition":
            "Branch based on a condition.",

        "function.block_description.compare":
            "Compare a value such as an OCR cooldown.",

        "function.block_description.duration_parse":
            "Parse a duration text (e.g. OCR cooldown) into seconds.",

        "function.block_description.due_date_remaining":
            "Calculate seconds until a due date; empty or overdue values return 0.",

        "function.block_description.gate":
            "Combine multiple boolean values.",

        "function.block_description.repeat":
            "Repeat a branch with iteration and time limits.",

        "function.block_description.stop_event":
            "Count arrivals, then end the current event and reschedule it.",

        "function.block_description.set_event_due":
            "Make another event in this account due now, without stopping this event.",

        "function.block_description.reschedule_event":
            "Schedule an event in this account (including the running one) after a delay in seconds.",

        "function.block_description.splitter":
            "Split execution branch into multiple parallel sub-branches.",

        "function.block_description.timer":
            "Check whether a cooldown has elapsed.",

        "function.block_description.variable":
            "Read or write a profile variable.",

        "function.block_description.configure_field":
            "Declare a configurable Event or Task field.",

        "function.block_description.negate_outcome":
            "Forward success as failure and failure as success.",

        "function.dialog.variable_name":
            "Profile variable name",

        "function.dialog.save_copy_title":
            "Save copy as",

        "function.dialog.function_name":
            "Function name:",

        # --------------------------
        # Variables
        # --------------------------

        "variables.create":
            "Create Variable",

        "variables.create_title":
            "Create Variable",

        "variables.name":
            "Name",

        "variables.description":
            "Description",

        "variables.kind":
            "Variable kind",

        "variables.kind_config":
            "Configuration",

        "variables.kind_config_description":
            "A desired profile setting. Functions may read it by default but may change it only when explicitly allowed.",

        "variables.kind_profile":
            "Profile value",

        "variables.kind_profile_description":
            "Stable profile data that remains valid until changed. Functions may read it by default but may change it only when explicitly allowed.",

        "variables.kind_live":
            "Live value",

        "variables.kind_live_description":
            "Mutable runtime state for this profile. It becomes stale on runtime start, profile switch, or recovery until refreshed. Functions may write it by default.",

        "variables.scope_profile_description":
            "Scope: available to Functions running for this Game and Profile. It is not tied to a specific Event or Task.",

        "variables.scope_event_description":
            "Scope: this Event. All Tasks in the Event can access the field; other Events cannot.",

        "variables.scope_task_description":
            "Scope: this Task only. Other Tasks and Events cannot access the field.",

        "variables.value_type":
            "Value type",

        "variables.type_text":
            "Text",

        "variables.type_number":
            "Number",

        "variables.type_boolean":
            "Yes / No",

        "variables.type_datetime":
            "Date and time",

        "variables.type_duration":
            "Duration in seconds",

        "variables.type_collection":
            "Collection (JSON list)",

        "variables.initial_value":
            "Initial value (optional)",

        "variables.function_writable":
            "Functions may change this value",

        "block.configure_field.how_to":
            "1. Enter the user-visible variable name.\n2. Event scope is shared by all Tasks in that Event; Task scope is limited to that Task.\n3. Config/Profile/Live describe the value's meaning and freshness, not its scope.\n4. Save the Function to register the declaration.",

        "function.block.math":
            "Math",

        "function.block_description.math":
            "Compute a numeric value from two inputs or variables.",

        "block.math.how_to":
            "1. Set left and right values or $variables.\n2. Choose +, -, * or /.\n3. The result is stored under the named output for the next block.\n4. Division by zero and non-numeric input return failed.",

        "variables.debug_visible":
            "Show in debug output",

        "variables.error_name_required":
            "A variable name is required.",

        "variables.error_exists":
            "A variable named '{name}' already exists for this game.",

        "variables.multi_account_description":
            "Written by the scheduler before switching profiles on a shared instance.",

        "variables.multi_account_profile_id_description":
            "Profile ID for the target profile selected by the scheduler.",

        # --------------------------
        # Learning
        # --------------------------

        "learning.region":
            "Region",

        "learning.label":
            "Label",

        "learning.element_name":
            "Element Name",

        "learning.detection":
            "Detection",

        "learning.tap":
            "Tap",

        "learning.ocr":
            "OCR",

        "learning.display":
            "Display",

        "learning.visible":
            "Visible",

        "learning.locked":
            "Locked",

        "learning.position":
            "Position",

        "learning.size":
            "Size",
 
        "learning.profile":
            "Profile",

        "learning.capture":
            "Capture",

        "learning.search_boundary":
            "Search Boundary",

        "learning.detection_pattern":
            "Detection Pattern",

        "learning.tap_pattern":
            "Tap Pattern",

        "learning.ocr_pattern":
            "OCR Pattern",

        "learning.color_trigger":
            "Color Trigger",

        "learning.delete_region":
            "Delete Region",

        # --------------------------
        # Runtime
        # --------------------------

        "runtime.title":
            "Runtime",

        "runtime.start":
            "Runtime Start",

        "runtime.start_queued":
            "Runtime start accepted; preparing profiles...",

        "runtime.start_complete":
            "Runtime is running.",

        "runtime.start_blocked":
            "Runtime did not start. Resolve the configuration issue above first.",

        "runtime.stop_requested":
            "Runtime stop requested...",

        "runtime.stop_complete":
            "Runtime stopped.",

        "runtime.operation_busy":
            "Another runtime operation is in progress. This request was not queued.",

        "runtime.operation_failed":
            "Runtime operation failed. See the runtime log for details.",

        "runtime.heartbeat_queued":
            "Refreshing runtime profiles...",

        "runtime.heartbeat_complete":
            "Runtime profiles refreshed.",

        "runtime.profile_enabled":
            "Profile enabled. It will be used at the next runtime start.",

        "runtime.profile_disabled":
            "Profile disabled. It will be excluded at the next runtime start.",

        "runtime.profile_change_rejected":
            "Profile change rejected. Review the conflict details.",

        "runtime.pause_complete":
            "Runtime paused. No further automation commands will be sent until resumed.",

        "runtime.paused":
            "Runtime Paused",

        "runtime.resume_complete":
            "Runtime resumed.",

        "runtime.resume_short":
            "Resume",

        "runtime.state_idle":
            "Idle",

        "runtime.state_unknown":
            "Unknown",

        "game_overview.device_value":
            "Device: {value}",

        "game_overview.connection_value":
            "Live session: {value}",

        "logs.refresh_failed":
            "Log refresh failed. See the Studio log for details.",

        "runtime.stop":
            "Runtime Stop",

        "runtime.start_short":
            "Start",

        "runtime.pause_short":
            "Pause",

        "runtime.stop_short":
            "Stop",

        "runtime.stopped":
            "Runtime Stopped",

        "runtime.emulator_stopped":
            "Emulator stopped",

        "runtime.running":
            "Runtime Running",

        "runtime.recovering":
            "Runtime Recovering",

        "runtime.error":
            "Runtime Error",

        "runtime.state_healthy":
            "Healthy",

        "runtime.state_recovering":
            "Recovering",

        "runtime.state_failed":
            "Failed",

        "runtime.state_queued":
            "Queued",

        "runtime.status_healthy_prefix":
            "Healthy",

        "runtime.status_recovering_prefix":
            "Recovering",

        "runtime.status_failed_prefix":
            "Failed",

        "runtime.status_queued_prefix":
            "Queued",

        "runtime.no_problems":
            "No runtime problems",

        "runtime.uptime":
            "Uptime",

        "runtime.uptime_zero":
            "Uptime: 00:00:00",

        "runtime.summary_state":
            "Runtime State",

        "runtime.summary_healthy":
            "Healthy",

        "runtime.summary_recovering":
            "Recovering",

        "runtime.summary_failed":
            "Failed",

        "runtime.summary_queued":
            "Queued",

        "runtime.manual_account_hint":
            "Select the matching in-game account yourself; this cannot be automated here.",

        "runtime.events":
            "Runtime Events",

        "runtime.profile_overview":
            "Runtime Profile Overview",

        "runtime.profile":
            "Profile Name",

        "runtime.game":
            "Game",

        "runtime.enabled":
            "Enabled",

        "runtime.state":
            "State",

        "runtime.message":
            "Message",

        "runtime.conflict_title":
            "Conflict detected",

        "runtime.conflict_text":
            "Emulator Instance {instance} is already used by:\n\n{profiles}\n\nGame:\n{games}\n\nDo you want to disable these profiles and enable {target} instead?",

        "runtime.conflict_replace":
            "Disable conflicting profiles and enable selected profile",

        "runtime.select_mumu_manager":
            "Select MuMuManager.exe",

        "runtime.package_missing_title":
            "Game package missing",

        "runtime.package_missing_text":
            "The following enabled profiles have no package configured:\n\n{profiles}\n\nOpen the game on the emulator, then use Game Setup to detect and save the package name.",

        # --------------------------
        # Game Overview / Setup
        # --------------------------

        "game_overview.overview":
            "Overview",

        "game_overview.detected_sessions":
            "Detected Emulator Sessions",

        "game_overview.instance":
            "Instance",

        "game_overview.running_app":
            "Running App",

        "game_overview.game":
            "Game",

        "game_setup.tab":
            "Game Setup",

        "game_setup.title":
            "Game Start Parameters",

        "game_setup.game_id":
            "Game ID",

        "game_setup.display_name":
            "Display Name",

        "game_setup.package_name":
            "Package Name",

        "game_setup.emulator_instance":
            "Emulator Instance",

        "game_setup.detect_package":
            "Detect Package",

        "game_setup.open_game_hint":
            "Open only the target game on the emulator, then detect the package name.",

        "game_setup.package_not_detected":
            "No package detected.",

        "game_setup.package_detected":
            "Package detected.",

        "game_setup.missing_fields":
            "Game ID, display name and package name are required.",

        "game_setup.saved":
            "Game saved.",

        # --------------------------
        # Logs
        # --------------------------

        "logs.title":
            "Logs",

        "logs.search":
            "Search",

        "logs.profile":
            "Profile",

        "logs.severity":
            "Severity",

        "logs.module":
            "Module",

        "logs.time":
            "Time",

        "logs.all_profiles":
            "All Profiles",

        "logs.all_severities":
            "All Severities",

        "logs.all_modules":
            "All Modules",

        "logs.all_time":
            "All Time",

        "logs.last_hour":
            "Last Hour",

        "logs.last_day":
            "Last 24 Hours",

        "logs.refresh":
            "Refresh",

        "logs.timestamp":
            "Timestamp",

        "logs.level":
            "Level",

        "logs.game":
            "Game",

        "logs.message":
            "Message",

        # --------------------------
        # OCR
        # --------------------------

        "ocr.title":
            "OCR",

        "ocr.type":
            "OCR Type",

        "ocr.validator":
            "Validator",

        "ocr.expected_pattern":
            "Expected Pattern",

        # --------------------------
        # Verification
        # --------------------------

        "verify.title":
            "Verification",

        "verify.score":
            "Score",

        "verify.not_verified":
            "Not verified",

        "verify.excellent":
            "Excellent",

        "verify.good":
            "Good",

        "verify.poor":
            "Poor",

        # --------------------------
        # Teach Results
        # --------------------------

        "teach.entry_id":
            "Entry ID",

        "teach.entry_name":
            "Entry Name",

        "teach.pattern_type":
            "Pattern Type",

        "teach.accepted_samples":
            "Accepted Samples",

        "teach.rejected_samples":
            "Rejected Samples",

        "teach.verify_best":
            "Verify Best",

        "teach.verify_average":
            "Verify Average",

        "teach.verify_worst":
            "Verify Worst",

        "teach.success_rate":
            "Success Rate",

        "teach.matrix":
            "Matrix",

        "teach.matrix_generated":
            "Generated",

        # -------------------------
        # Profiles
        # -------------------------

        "profile.title":
            "Profiles",

        "profile.details":
            "Profile Details",

        "profile.id":
            "Profile ID",
        
        "profile.name":
            "Name",

        "profile.enabled":
            "Enabled",

        "profile.game":
            "Game",

        "profile.emulator":
            "Emulator Instance",

        "profile.priority":
            "Priority",

        "profile.existing":
            "Existing Profiles",

        "profile.delete_title":
            "Delete Profile",

        "profile.delete_text":
            "Do you really want to delete this profile?",

        "profile.unsaved_title":
            "Unsaved Changes",

        "profile.unsaved_text":
            "Discard unsaved changes?",

        "profile.validation_title":
            "Validation",

        "profile.validation_id":
            "Profile ID is required.",

        "profile.validation_name":
            "Profile name is required.",

        "profile.label_id":
            "ID",

        "profile.label_name":
            "Name",

        "profile.game_freeze_title":
            "Bind game permanently",

        "profile.game_freeze_text":
            "The selected game will be permanently bound to this profile after saving and cannot be changed. Continue?",

        "profile.game_frozen_title":
            "Game is frozen",

        "profile.game_frozen_text":
            "The game of a saved profile cannot be changed.",

        "teach.missing_name_text":
            "Element name is missing. Save anyway?",

        "teach.no_preview":
            "No teach preview",

        "scheduler.title":
            "Scheduler",

        "scheduler.game":
            "Game",

        "scheduler.table_view":
            "Table view",

        "scheduler.timeline_view":
            "Timeline view",

        "scheduler.view_mode_tooltip":
            "Switch between Table and Timeline views",

        "scheduler.event_filter_all":
            "All events",

        "scheduler.event_filter_tooltip":
            "Show only this event in all profiles (reset when switching view or page)",

        "scheduler.refresh":
            "Refresh",

        "scheduler.add_event":
            "Add event",

        "scheduler.add_task":
            "Add task",

        "scheduler.edit_selected":
            "Edit selected",

        "scheduler.toggle_enabled":
            "Toggle enabled",

        "scheduler.delete_selected":
            "Delete selected",

        "scheduler.run_selected":
            "Run selected",

        "scheduler.event_queued":
            "Event saved in the queue for the next available scheduler slot.",

        "scheduler.event_already_running":
            "This event is already queued or running. Wait until it finishes before starting it again.",

        "scheduler.event_failed":
            "Event failed before it could finish. See the scheduler log for details.",

        "scheduler.event_not_found":
            "The selected event no longer exists. Refresh the scheduler.",

        "scheduler.event_missing_profile":
            "Cannot run this event: assign it to an existing profile first.",

        "scheduler.event_no_active_tasks":
            "This event has no active tasks. Enable a task with a function, then run it again.",

        "scheduler.select_event_to_run":
            "Select an event before choosing Run selected.",

        "scheduler.select_event_to_cancel":
            "Select a running event before choosing Cancel selected.",

        "scheduler.cancel_unavailable":
            "This event cannot be cancelled.",

        "scheduler.cancel_requested":
            "Cancellation requested. The event will stop at its next safe checkpoint.",

        "scheduler.cancel_not_running":
            "The event is not currently running, so there is nothing to cancel.",

        "scheduler.runtime_required":
            "Start Runtime before running a selected event.",

        "scheduler.cancel_selected":
            "Cancel selected",

        "scheduler.no_event_selected":
            "No event selected",

        "scheduler.no_game_selected":
            "No game selected",

        "scheduler.select_game_before_event":
            "Select a game before adding an event",

        "scheduler.sync_profiles":
            "Sync profiles",

        "scheduler.sync_profiles_tooltip":
            "Replace custom scheduler events on selected profiles with copies from a source profile.",

        "scheduler.select_game_before_sync":
            "Select a game before synchronizing profiles.",

        "scheduler.sync_requires_two_profiles":
            "At least two profiles in this game are required to synchronize.",

        "scheduler.sync_source_profile":
            "Source profile",

        "scheduler.sync_target_profiles":
            "Target profiles (active and inactive)",

        "scheduler.profile_enabled":
            "active",

        "scheduler.profile_disabled":
            "inactive",

        "scheduler.sync_select_targets":
            "Select at least one target profile.",

        "scheduler.sync_replace_title":
            "Replace target scheduler events?",

        "scheduler.sync_replace_text":
            "Custom scheduler events on these profiles will be replaced: {targets}. Additional Init tasks are replaced too; protected Init tasks and Interrupt remain unchanged. Continue?",

        "scheduler.sync_in_progress":
            "Synchronizing scheduler profiles…",

        "scheduler.sync_failed":
            "Profile synchronization failed: {error}",

        "scheduler.sync_complete":
            "Copied {copied} events and {init_tasks} additional Init tasks to {targets} profiles; replaced {replaced} custom events.",

        "scheduler.sync_events":
            "Events to sync",

        "scheduler.sync_events_tooltip":
            "Unchecked events are not copied; the target keeps its own event with the same name.",

        "scheduler.unnamed_event":
            "(unnamed event)",

        "scheduler.sync_excluded_text":
            "Excluded (stay unchanged): {events}",

        "scheduler.sync_skipped":
            "{skipped} events excluded.",

        "scheduler.all_events_off":
            "All events off",

        "scheduler.all_events_on":
            "All events on",

        "scheduler.all_events_off_tooltip":
            "Disable every event of all profiles of this game (Init and Interrupt stay).",

        "scheduler.all_events_on_tooltip":
            "Enable every event of all profiles of this game.",

        "scheduler.all_events_off_confirm":
            "Disable all events of all profiles of this game?",

        "scheduler.all_events_on_confirm":
            "Enable all events of all profiles of this game?",

        "scheduler.profile_actions":
            "Profile actions",

        "scheduler.profile_events_off":
            "Disable all events of this profile",

        "scheduler.profile_events_on":
            "Enable all events of this profile",

        "scheduler.events_switched_on":
            "{count} events switched on.",

        "scheduler.events_switched_off":
            "{count} events switched off.",

        "scheduler.profiles":
            "Profiles",

        "scheduler.all_profiles_of_game":
            "All profiles of this game",

        "scheduler.create_for_multiple_profiles":
            "Create for several profiles",

        "scheduler.create_for_multiple_profiles_tooltip":
            "Creates an independent copy of the event (and task) for every checked profile.",

        "scheduler.task_name_optional":
            "Task name (optional, default: event name)",

        "scheduler.initial_task":
            "Task with functions (optional)",

        "scheduler.event_created_for_profiles":
            "Event \"{name}\" created for {count} profiles.",

        "scheduler.event_name_required":
            "Please enter an event name.",

        "scheduler.select_profiles_required":
            "Please check at least one profile.",

        "scheduler.mode_manual_label":
            "manual",

        "scheduler.mode_fixed_label":
            "fixed (interval / times)",

        "function.create_event":
            "Create event",

        "function.create_event_button":
            "Create event?",

        "function.create_event_tooltip":
            "Create a scheduler event for one, several or all profiles of this game. Its event variable is usable in the function right away.",

        "function.create_event_add_task":
            "Add \"{name}\" as task",

        "function.create_event_no_profiles":
            "This game has no profiles yet.",

        "scheduler.editor_hint":
            "Scheduler editor for saved events and trigger flow.",

        "scheduler.fields":
            "Fields",

        "scheduler.actions":
            "Actions",

        "scheduler.more_actions":
            "More actions",

        "scheduler.field_inspector":
            "Field details",

        "scheduler.field_inspector_for":
            "Field details · {name}",

        "scheduler.field_defined_by":
            "Defined by: {functions}",

        "scheduler.field_from_helper":
            " (helper)",

        "scheduler.field_from_helper_hint":
            "Declared by a helper function; the value is set here on the caller.",

        "scheduler.field_contract":
            "Kind: {kind} · Type: {value_type}",

        "scheduler.select_field_value":
            "Select value",

        "scheduler.edit_collection":
            "Edit collection ({count})",

        "scheduler.edit_collection_title":
            "Edit collection",

        "scheduler.collection_json":
            "JSON list",

        "scheduler.field_saved":
            "Field value saved.",

        "scheduler.field_save_failed":
            "Field value could not be saved: {error}",

        "scheduler.confirm_field":
            "Confirm",

        "scheduler.available_functions":
            "Available Functions",

        "scheduler.event_functions":
            "Event Functions",

        "scheduler.event_name":
            "Event name",

        "scheduler.task_name":
            "Task name",

        "scheduler.profile_id":
            "Profile ID",

        "scheduler.priority":
            "Priority",

        "scheduler.group_id":
            "Group ID",

        "scheduler.group_order":
            "Group order",

        "scheduler.group_execution_order":
            "Group execution order",

        "scheduler.schedule_mode":
            "Schedule mode",

        "scheduler.interval_seconds":
            "Interval seconds",

        "scheduler.interval_time":
            "Interval (HH:MM:SS)",

        "scheduler.local_times":
            "Local times (HH:MM, comma separated)",

        "scheduler.functions_in_order":
            "Functions in order",

        "settings.title":
            "Settings",

        "settings.game":
            "Game",

        "settings.telegram_title":
            "Telegram Remote Control",

        "settings.telegram_enable":
            "Enable Telegram",

        "settings.telegram_description":
            "Control BotStudio remotely with Telegram commands. A bot token and both allowed-ID lists are required.",

        "settings.telegram_token":
            "Bot Token",

        "settings.telegram_token_placeholder":
            "Enter bot token from @BotFather",

        "settings.telegram_token_saved":
            "Token saved in Windows Credential Manager",

        "settings.telegram_show_token":
            "Show token",

        "settings.telegram_forget_token":
            "Forget token",

        "settings.telegram_user_ids":
            "User IDs",

        "settings.telegram_chat_ids":
            "Chat IDs",

        "settings.watchdog_path":
            "Watchdog Path",

        "settings.watchdog_path_hint":
            "Automatically detected from the current BotStudio installation.",

        "settings.watchdog_auto_start":
            "Auto-start",

        "settings.watchdog_auto_start_hint":
            "Start only the watchdog when this Windows user signs in. BotStudio waits for a start command.",

        "settings.telegram_runtime_status":
            "Status",

        "settings.telegram_feedback":
            "Connection",

        "settings.telegram_status":
            "{state} | Auto-start: {auto_start}",

        "settings.telegram_running":
            "Running",

        "settings.telegram_stopped":
            "Stopped",

        "settings.telegram_registered":
            "Registered",

        "settings.telegram_not_registered":
            "Not registered",

        "settings.telegram_test_connection":
            "Test Connection",

        "settings.telegram_test_connection_short":
            "Test",

        "settings.telegram_save":
            "Save Configuration",

        "settings.telegram_save_short":
            "Save",

        "settings.telegram_connected":
            "Connected to @{username}",

        "settings.telegram_windows_only":
            "Telegram credentials and login startup require Windows.",

        "settings.telegram_settings_load_failed":
            "Could not load Telegram configuration: {error}",

        "settings.watchdog_status_failed":
            "Could not read watchdog status: {error}",

        "settings.telegram_token_required":
            "Enter a bot token before testing the connection.",

        "settings.telegram_testing":
            "Testing Telegram connection...",

        "settings.telegram_test_failed":
            "Connection failed: {error}",

        "settings.telegram_save_failed":
            "Could not apply configuration: {error}",

        "settings.telegram_saved":
            "Telegram and watchdog configuration saved.",

        "settings.instance_settings":
            "Instance Settings",

        "settings.idle_behavior":
            "Idle Behavior",

        "settings.max_idle_minutes":
            "Max Idle Time (minutes)",

        "settings.max_concurrent_instances":
            "Max Concurrent Instances",

        "settings.limit_profile_active_time":
            "Limit Profile Active Time",

        "settings.max_active_minutes":
            "min",

        "settings.stop_behavior":
            "Stop Behavior",

        "settings.auto_start_all_profiles":
            "Auto start all Profiles after module load",

        "settings.stop_behavior_telegram":
            "Stop Behavior Telegram",

        "settings.delay":
            "Delay:",

        "settings.mode":
            "Mode:",

        "settings.game_settings":
            "Game Settings",

        "settings.game_version":
            "Game Version",

        "settings.startup_screen":
            "Startup Screen",

        "settings.application_startup_view":
            "Application startup screen",

        "settings.application_startup_view_tooltip":
            "Select the first screen shown when BotStudio opens. Runtime start does not change this setting.",

        "settings.no_startup_screen":
            "No startup screen",

        "settings.startup_screen_home":
            "Home",

        "settings.profile_functions_hint":
            "Profile Check and Profile Switch are protected Init Tasks. "
            "Assign their functions per profile in the Scheduler. "
            "The target account name is provided as {variable}.",

        "settings.option_do_nothing":
            "Do Nothing",

        "settings.option_close_game":
            "Close Game",

        "settings.option_close_emulator":
            "Close Emulator",

        "settings.option_continuous":
            "Continuous",

        "settings.option_once":
            "Once",

        "settings.select_switch_function":
            "Select switch function",

        "settings.select_validation_function":
            "Select validation function",

        "settings.saved":
            "Settings saved",

    }
}

_LOCALE_ROOT = Path(__file__).with_name("locales")
for _language_code, _language_name, _flag_code in LANGUAGES:
    if _language_code == "en":
        continue
    _locale_file = _LOCALE_ROOT / f"{_language_code}.json"
    try:
        _locale_values = json.loads(_locale_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _locale_values = {}
    STRINGS[_language_code] = (
        _locale_values if isinstance(_locale_values, dict) else {}
    )


def t(
    key: str,
):

    return (
        STRINGS
        .get(LANG, {})
        .get(key, STRINGS["en"].get(key, key))
    )
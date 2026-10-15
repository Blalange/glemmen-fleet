# Template for the Netbird extension service config.
#
# gen-config.sh renders this file to talos/generated/patches/netbird.yaml,
# substituting @NB_SETUP_KEY@ with the value from talos/secrets/netbird.env.
# The rendered file (containing the key) is gitignored.
apiVersion: v1alpha1
kind: ExtensionServiceConfig
name: netbird
environment:
    - NB_SETUP_KEY=@NB_SETUP_KEY@
    - NB_MANAGEMENT_URL=https://netbird.blalange.org:443
    - NB_ADMIN_URL=https://netbird.blalange.org:443

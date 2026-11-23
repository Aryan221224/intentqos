# OpenWrt / prplOS package feed entry for IntentQoS.
# Use with the OpenWrt SDK:  ./scripts/feeds ...; make package/intentqos/compile
# (router/build_ipk.py builds the same .ipk files without the SDK.)
include $(TOPDIR)/rules.mk

PKG_NAME:=intentqos
PKG_VERSION:=2.0.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_MAINTAINER:=IntentQoS project

include $(INCLUDE_DIR)/package.mk

define Package/intentqos
  SECTION:=net
  CATEGORY:=Network
  TITLE:=IntentQoS adaptive QoS engine (intent-aware, latency-driven)
  DEPENDS:=+python3-light +python3-email +python3-urllib +python3-openssl +python3-logging \
           +tc-full +kmod-sched-core +kmod-sched-cake +nftables +iq-capture
  PKGARCH:=all
endef

define Package/iq-capture
  SECTION:=net
  CATEGORY:=Network
  TITLE:=IntentQoS header-only flow accounting helper (TPACKET_V3)
endef

define Package/intentqos/conffiles
/etc/config/intentqos
endef

define Build/Compile
	$(TARGET_CC) $(TARGET_CFLAGS) -O2 -Wall -o $(PKG_BUILD_DIR)/iq-capture ./src/iq-capture.c
endef

define Package/iq-capture/install
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/iq-capture $(1)/usr/sbin/
endef

define Package/intentqos/install
	$(INSTALL_DIR) $(1)/usr/lib/intentqos/intentqos/web $(1)/usr/lib/intentqos/models $(1)/etc/config $(1)/etc/init.d $(1)/usr/sbin
	$(CP) ./src/intentqos/*.py $(1)/usr/lib/intentqos/intentqos/
	$(CP) ./src/intentqos/web/console.html $(1)/usr/lib/intentqos/intentqos/web/
	$(CP) ./src/models/flowclf.iqm $(1)/usr/lib/intentqos/models/
	$(INSTALL_CONF) ./files/intentqos.config $(1)/etc/config/intentqos
	$(INSTALL_BIN) ./files/intentqos.init $(1)/etc/init.d/intentqos
	$(INSTALL_BIN) ./files/intentqos-uci $(1)/usr/sbin/intentqos-uci
	$(INSTALL_DIR) $(1)/etc/hotplug.d/dhcp
	$(INSTALL_BIN) ./src/kit/files/etc/hotplug.d/dhcp/90-intentqos $(1)/etc/hotplug.d/dhcp/90-intentqos
	$(CP) ./src/iq-flow.bpf.o ./src/iq-flow-eb.bpf.o $(1)/usr/lib/intentqos/ 2>/dev/null || true
endef

$(eval $(call BuildPackage,iq-capture))
$(eval $(call BuildPackage,intentqos))

# First-boot settings a GL-MT3000 owner ends up with (typed on the router's console by systemsim).
uci set system.@system[0].hostname='GL-MT3000'
uci set system.@system[0].zonename='Asia/Kolkata'
uci set system.@system[0].timezone='IST-5:30'
uci set network.lan.ipaddr='192.168.8.1/24'
uci del_list network.@device[0].ports='eth2' 2>/dev/null; uci add_list network.@device[0].ports='eth2'
uci del_list network.@device[0].ports='eth3' 2>/dev/null; uci add_list network.@device[0].ports='eth3'
uci commit
(echo 'labrouter1'; echo 'labrouter1') | passwd root >/dev/null 2>&1
# the emulated internet has no TLS for downloads.openwrt.org: same release feeds over plain HTTP via the
# lab mirror (package and index signatures are still verified)
[ -f /etc/apk/repositories.d/distfeeds.list ] && sed -i 's#https://downloads.openwrt.org#http://downloads.openwrt.org#' /etc/apk/repositories.d/distfeeds.list
[ -f /etc/opkg/distfeeds.conf ] && sed -i 's#https://downloads.openwrt.org#http://downloads.openwrt.org#' /etc/opkg/distfeeds.conf
/etc/init.d/system reload
/etc/init.d/network restart

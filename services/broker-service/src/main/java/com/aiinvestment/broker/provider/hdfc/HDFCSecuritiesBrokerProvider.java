package com.aiinvestment.broker.provider.hdfc;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.HDFCSecuritiesProviderProperties;
import com.aiinvestment.broker.security.BrokerCredentialStore;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;
import java.math.BigDecimal;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.EnumSet;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

@Component
public class HDFCSecuritiesBrokerProvider implements BrokerProvider {
    private final HDFCSecuritiesProviderProperties properties;
    private final BrokerCredentialStore credentials;
    private final HDFCSessionStore sessions;
    private final HDFCReadClient client;
    public HDFCSecuritiesBrokerProvider(HDFCSecuritiesProviderProperties properties, BrokerCredentialStore credentials,
                                        HDFCSessionStore sessions, HDFCReadClient client) {
        this.properties=properties; this.credentials=credentials; this.sessions=sessions; this.client=client;
    }
    public BrokerType supportedBroker(){ return BrokerType.HDFC_SECURITIES; }
    public BrokerConnectionCapabilities connectionCapabilities(){ return BrokerConnectionCapabilities.none(); }
    public BrokerConnectionCapabilities connectionCapabilities(UUID u, UUID c){ return connectionStatus(u,c).state()==BrokerConnectionState.CONNECTED
            ? new BrokerConnectionCapabilities(EnumSet.of(BrokerCapability.ACCOUNTS_READ,BrokerCapability.ACCOUNT_METADATA_READ,
            BrokerCapability.POSITIONS_READ,BrokerCapability.CASH_READ,BrokerCapability.PORTFOLIO_READ)) : BrokerConnectionCapabilities.none(); }
    public BrokerConnectionStatus connectionStatus(){ return BrokerConnectionStatus.authenticationRequired(BrokerType.HDFC_SECURITIES); }
    public BrokerConnectionStatus connectionStatus(UUID u, UUID c){ var session=sessions.find(u,c);
        if(session.isEmpty()) return BrokerConnectionStatus.authenticationRequired(BrokerType.HDFC_SECURITIES);
        if(session.get().expired(Instant.now())) return new BrokerConnectionStatus(BrokerType.HDFC_SECURITIES,
                BrokerConnectionState.SESSION_EXPIRED,BrokerProviderStatus.AUTHENTICATION_REQUIRED,"SESSION_EXPIRED","HDFC Securities authentication has expired.");
        return new BrokerConnectionStatus(BrokerType.HDFC_SECURITIES,BrokerConnectionState.CONNECTED,
                BrokerProviderStatus.CONNECTED,"CONNECTED","HDFC Securities is connected."); }
    public String login(UUID u, UUID c){ var cr=requireCredentials(u,c); URI base=URI.create(properties.loginUrl());
        if(!"https".equalsIgnoreCase(base.getScheme())||!"developer.hdfcsec.com".equalsIgnoreCase(base.getHost())||!"/oapi/v1/login".equals(base.getPath()))
            throw BrokerProviderException.documentationRequired(BrokerType.HDFC_SECURITIES);
        sessions.markLoginInitiated(u,c); return properties.loginUrl()+"?api_key="+java.net.URLEncoder.encode(cr.clientKey(), StandardCharsets.UTF_8); }
    public BrokerConnectionStatus attachRequestToken(UUID u, UUID c, String token){ if(token==null||token.isBlank()||!sessions.consumeLoginInitiated(u,c))
            throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES);
        var cr=requireCredentials(u,c); String access=client.exchangeAccessToken(cr.clientKey(),cr.clientSecret(),token);
        Instant now=Instant.now(); sessions.store(new HDFCConnectorSession(u,c,access,now,now.plusSeconds(Math.max(300,properties.sessionTtlSeconds()))));
        return connectionStatus(u,c); }
    public List<BrokerAccount> fetchAccounts(UUID u){ throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES); }
    public List<BrokerAccount> fetchAccounts(UUID u, UUID c){ var s=require(u,c); var cr=requireCredentials(u,c); var p=client.profile(cr.clientKey(),s.accessToken());
        String id="HDFC_SECURITIES:"+UUID.nameUUIDFromBytes(p.userId().getBytes(StandardCharsets.UTF_8));
        return List.of(new BrokerAccount(id,u,BrokerType.HDFC_SECURITIES,"***"+p.userId().substring(Math.max(0,p.userId().length()-4)),p.userName(),"INR",BrokerAccountStatus.ACTIVE)); }
    public List<BrokerPosition> fetchPositions(BrokerAccount a){ throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES); }
    public List<BrokerPosition> fetchPositions(UUID u, UUID c, BrokerAccount a){ requireAccount(u,a); var s=require(u,c); var cr=requireCredentials(u,c); Instant observed=Instant.now();
        return client.holdings(cr.clientKey(),s.accessToken()).stream().filter(h->h.quantity()!=null&&h.quantity().signum()>=0).map(h->{
            String isin=normalizeIsin(h.isin()); String external=isin==null?"SECURITY:"+h.securityId():"ISIN:"+isin;
            Instrument i=new Instrument(UUID.nameUUIDFromBytes(("HDFC_SECURITIES|"+external).getBytes(StandardCharsets.UTF_8)),"HDFC_SECURITIES",external,isin,
                    h.companyName()==null?h.securityId():h.companyName(),h.exchange(),"INR",h.companyName(),AssetType.EQUITY,"IN",null,null,null);
            BigDecimal value=h.closePrice()==null?null:h.closePrice().multiply(h.quantity()); Money mv=value==null?null:new Money(value,"INR");
            Money average=h.averagePrice()==null?null:new Money(h.averagePrice(),"INR");
            Money current=h.closePrice()==null?null:new Money(h.closePrice(),"INR");
            return new BrokerPosition(a.brokerAccountId(),i,h.quantity(),average,current,mv,null,observed); }).toList(); }
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount a){ throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES); }
    public List<BrokerCashBalance> fetchCashBalances(UUID u, UUID c, BrokerAccount a){ requireAccount(u,a); var s=require(u,c); var cr=requireCredentials(u,c); var f=client.funds(cr.clientKey(),s.accessToken());
        return List.of(new BrokerCashBalance(a.brokerAccountId(),new Money(f.cash(),"INR"),null,null,null,null,null,"INVESTRIGHT_AVAILABLE_CASH")); }
    public void disconnect(UUID c){}
    public void disconnect(UUID u,UUID c){ sessions.remove(u,c); }
    private HDFCConnectorSession require(UUID u,UUID c){ return sessions.find(u,c).filter(s->!s.expired(Instant.now())).orElseThrow(()->BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES)); }
    private BrokerCredentialStore.BrokerCredentials requireCredentials(UUID u,UUID c){ return credentials.find(u,c,BrokerType.HDFC_SECURITIES).orElseThrow(()->BrokerProviderException.secretUnavailable(BrokerType.HDFC_SECURITIES)); }
    private static void requireAccount(UUID u,BrokerAccount a){ if(a==null||!u.equals(a.userId())||a.brokerType()!=BrokerType.HDFC_SECURITIES) throw BrokerProviderException.authenticationRequired(BrokerType.HDFC_SECURITIES); }
    private static String normalizeIsin(String v){ if(v==null)return null; String n=v.replaceAll("\\s+","").toUpperCase(Locale.ROOT); return n.matches("[A-Z]{2}[A-Z0-9]{9}[0-9]")?n:null; }
}

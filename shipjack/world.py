"""One self-contained shopping world: stores, a processor, a credential provider and a trusted surface."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Product
from .mandates import TrustList
from .merchant import Merchant
from .psp import CredentialProvider, Processor
from .surface import Intent, TrustedSurface
from .wallet import Wallet


@dataclass
class Store:
    id: str
    name: str
    website: str
    products: list[Product]
    impostor_of: str | None = None  # id of the store whose identity this one claims in its checkouts
    surcharge: int = 0  # cents added at completion, above the price shown
    verifies_mandates: bool = True


@dataclass
class World:
    processor: Processor
    credentials: CredentialProvider
    surface: TrustedSurface
    merchants: dict[str, Merchant]
    directory: dict = field(default_factory=dict)  # merchant id -> published checkout key

    def wallet(self, profile: str, intent: Intent) -> Wallet:
        wallet = Wallet(profile, self.credentials, surface=self.surface, directory=self.directory)
        if profile != "card":
            wallet.delegation = self.surface.delegate(intent, wallet.agent_public_key(),
                                                      extensions=profile == "ap2+ext")
        return wallet

    def info(self, store_id: str) -> dict:
        return dict(self.merchants[store_id].info)


def build(stores: list[Store]) -> World:
    surface = TrustedSurface()
    trust = TrustList([surface.verification_key()])
    processor = Processor()
    credentials = CredentialProvider(trust)
    processor.credentials = credentials
    merchants: dict[str, Merchant] = {}
    for s in stores:
        claimed = merchants[s.impostor_of].info if s.impostor_of else None
        merchants[s.id] = Merchant(s.id, s.name, s.website, s.products, processor, trust, impostor_of=claimed,
                                   surcharge=s.surcharge, verifies_mandates=s.verifies_mandates)
    directory = {m.id: m.verification_key() for m in merchants.values() if m.claimed == m.info}
    return World(processor, credentials, surface, merchants, directory)

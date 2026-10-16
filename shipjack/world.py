"""One self-contained shopping world: stores, a processor, a credential provider and a trusted surface."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Product
from .mandates import TrustList
from .merchant import Merchant
from .psp import CredentialProvider, Processor
from .store import Database, LedgerCharges, LedgerRefunds, SqliteRepo
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


def build(stores: list[Store], processor: Processor | None = None, db: str | Database | None = None) -> World:
    """``db`` (a SQLite path) keeps the merchants' state and the processor's ledger in that file."""
    surface = TrustedSurface()
    trust = TrustList([surface.verification_key()])
    processor = processor if processor is not None else Processor()
    if db is not None and not isinstance(db, Database):
        db = Database(db)
    if db is not None:
        processor.charges, processor.refunds = LedgerCharges(db), LedgerRefunds(db)
    credentials = CredentialProvider(trust)
    processor.credentials = credentials
    merchants: dict[str, Merchant] = {}
    for s in stores:
        claimed = merchants[s.impostor_of].info if s.impostor_of else None
        merchants[s.id] = Merchant(s.id, s.name, s.website, s.products, processor, trust, impostor_of=claimed,
                                   surcharge=s.surcharge, verifies_mandates=s.verifies_mandates,
                                   repo=SqliteRepo(db, s.id) if db is not None else None)
    directory = {m.id: m.verification_key() for m in merchants.values() if m.claimed == m.info}
    return World(processor, credentials, surface, merchants, directory)

import gzip
import pickle

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Column
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.types import String, CLOB


def file_to_object(save_file):
    print("Reading database")
    fp = None
    try:
        fp = gzip.open(save_file, 'rb')
        object = pickle.load(fp)
    except IOError as ioe:
        print(ioe.strerror)
        return None
    finally:
        if fp:
            fp.close()
    return object


Base = declarative_base()


class Match(Base):
    __tablename__ = "match"

    id = Column(String, primary_key=True)
    data = Column(CLOB)


def get_session(name):
    engine = create_engine(
        f"sqlite:///{name}",
        echo=False
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    return session

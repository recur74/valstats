import os
import gzip
import pickle


def file_to_object(save_file):
    print("Reading database")
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


def object_to_file(object, filename):
    print("Saving to database")
    try:
        if os.path.exists(f"{filename}.bak"):
            os.remove(f"{filename}.bak")
        if os.path.exists(filename):
            os.rename(filename, f"{filename}.bak")
        fp = gzip.open(filename, 'wb')
        pickle.dump(object, fp, protocol=2)
        if os.path.getsize(filename) == 0:
            print("Failed to save to database")
            os.remove(filename)
            if os.path.exists(f"{filename}.bak"):
                os.rename(f"{filename}.bak", filename)
    except BaseException as e:
        if fp:
            fp.close()
        print("Failed to save to database")
        os.remove(filename)
        if os.path.exists(f"{filename}.bak"):
            os.rename(f"{filename}.bak", filename)
    finally:
        fp.close()

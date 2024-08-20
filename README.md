# pip-ext

[***`pip-ext`***](https://github.com/l1asis/pip-ext) is a script intended to extend (or improve) the functionality of the `pip` Python package manager for my own purposes.

## **Install**
```bash
git clone https://github.com/l1asis/pip-ext.git
cd ./pip-ext
pip install .
```

## **Usage**
```bash
pip-ext search "requests" -v "2.23.0"
```
```
Name: requests
Version: 2.23.0
Summary: Python HTTP for Humans.
Author-email: mailto:me@kennethreitz.org
Author: Kenneth Reitz
Requires: Python >=2.7,  !=3.0.*,  !=3.1.*,  !=3.2.*,  !=3.3.*,  !=3.4.*
Links:
  Homepage: https://requests.readthedocs.io
  Documentation: https://requests.readthedocs.io
  Source: https://github.com/psf/requests
Dependencies: {'chardet>=3.0.2,<4', 'urllib3>=1.21.1,<1.26,!=1.25.0,!=1.25.1', 'certifi>=2017.4.17', 'idna>=2.5,<3'}
```